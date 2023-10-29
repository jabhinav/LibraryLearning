import torch
import wandb
from torch.utils.data import DataLoader, RandomSampler
from tqdm import tqdm
from transformers import get_constant_schedule_with_warmup
from transformers.modeling_utils import unwrap_model

from custom_peft import get_peft_model, PromptTuningInit, PromptTuningConfig, TaskType
from utils.config import get_config
from utils.data import MBPP_Dataset as CustomDataset
from utils.xformer import load_tokenizer, load_base_model
from utils.model import get_response_log_probs, compute_responsibilities


def learn(args, logger):
	# Initialise wandb
	if args.wandb_logging:
		wandb.init(project=args.project_name, config=vars(args))
	
	# Get the tokenizer
	tokenizer = load_tokenizer(args.model_type, args.tokenizer_name)
	
	# Get the config
	peft_config = PromptTuningConfig(
		task_type=TaskType.MULTI_CAUSAL_LM,  # CAUSAL_LM, SEQ_2_SEQ_LM for Dec-only, Enc-Dec. MULTI is my custom field.
		prompt_tuning_init=PromptTuningInit.RANDOM,  # TEXT for text, RANDOM for random
		num_virtual_tokens=args.num_virtual_tokens,
		# prompt_tuning_init_text="Classify if tweet is a complaint or not:",  # Use this if prompt_tuning_init is TEXT
		# tokenizer_name_or_path=args.model_name_or_path,  # Use this if prompt_tuning_init is TEXT
		num_init_clusters=args.num_libraries,  # My custom field
	)
	
	# Get the dataset
	dataset = CustomDataset(
		path_to_data=args.path_to_data,
		tokenizer=tokenizer,
		max_prompt_length=args.max_prompt_length,
		max_length=args.max_length,
		sample_problems=args.num_train_problems,
		mode='train'
	)
	
	args.batch_size = args.per_gpu_train_batch_size * max(1, args.n_gpu)
	
	# Prepare training data loader
	sampler = RandomSampler(dataset)
	train_dataloader = DataLoader(dataset, sampler=sampler, batch_size=args.batch_size, num_workers=0, pin_memory=False)
	args.num_training_steps = (len(train_dataloader) * args.num_epochs)
	
	# Get the model
	_, model = load_base_model(
		model_type=args.model_type,
		config_name=args.config_name,
		model_path=args.model_name_or_path,
		load_in_8bit=args.load_in_8bit
	)
	
	# Load checkpoint
	if args.load_base_from_path is not None:
		# We load the model state dict on the CPU to avoid an OOM error.
		loaded_state_dict = torch.load(args.load_base_from_path, map_location="cpu")
		loaded_state_dict = {k.replace('module.', ''): v for k, v in loaded_state_dict.items()}
		model.load_state_dict(loaded_state_dict, strict=True)
		
		# release memory
		del loaded_state_dict
		
		# Log the loaded checkpoint
		msg = "Loaded model checkpoint from path: {}".format(args.load_base_from_path)
		logger.info(msg)
		print(msg)
	
	model = get_peft_model(model, peft_config)
	
	trainable_params, all_param = model.get_nb_trainable_parameters()
	msg = f"trainable params: {trainable_params:,d} || all params: {all_param:,d} || trainable%: {100 * trainable_params / all_param}"
	logger.info(msg)
	print(msg)  # Prompt tuning: embedding_dim * num_virtual_tokens * num_libraries
	
	# GPU-ize the model
	model.to(args.device)
	if args.n_gpu > 1:
		model = torch.nn.DataParallel(model)
	
	# Get the optimizer
	optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr)
	lr_scheduler = get_constant_schedule_with_warmup(
		optimizer=optimizer,
		num_warmup_steps=0,
	)
	
	logger.info("Starting EM for Library Learning")
	
	# # Debug Load the model
	# model.load_adapter(model_id=args.save_at, adapter_name='default')
	
	# ######################################### Initialisation for EM ############################################## #
	# Initialise the model parameters i.e. latent prompt embeddings for each library
	# This is equivalent to latching each library to a random sample from the dataset
	if args.pre_num_iters > 0:
		rdm_idxs = torch.randint(0, len(dataset), (args.num_libraries,))
		for k in range(args.num_libraries):
			
			logger.info("Initialisation for Library %d", k)
			for i in tqdm(range(args.pre_num_iters), desc=f"Init. Iterations Lib {k}", position=0, leave=True):
				batch = dataset.sample(rdm_idxs[k])
				batch = tuple(t.unsqueeze(0).to(args.device) for t in batch)
				
				# Get the response log-probability of the sample coming from the latent prompt of library k
				resp_log_prob = get_response_log_probs(args, batch, tokenizer, model, k)
				
				loss = -resp_log_prob.sum()  # responsibility = 1 for the sample coming from the latent prompt of library k
				
				# Update the model parameters
				loss.backward()
				optimizer.step()
				lr_scheduler.step()  # Make sure this is constant schedule with no warmup
				optimizer.zero_grad()
				
				logger.info(f"Iter {i} Loss: {loss.detach().cpu().numpy().item()}")
	
	# ################################################## EM ####################################################### #
	# Let's do EM to update the model with prompt-tuning
	for _ in tqdm(range(args.num_epochs), desc="Epochs", position=0, leave=True):
		for _ in tqdm(range(len(train_dataloader)), desc="EM Iterations", position=0, leave=True):
			
			# ############################################### E-Step #################################################### #
			# E-Step: Compute responsibilities corresponding to each program coming from some latent prompt of a library
			batch = next(iter(train_dataloader))
			batch = tuple(t.to(args.device) for t in batch)
			
			# Posterior probabilities of the sample coming from the latent prompt of each library := p(z_k|x_n)
			responsibilities = compute_responsibilities(args, batch, tokenizer, model)
			
			# To prevent underflow, clip the responsibilities to a minimum value
			responsibilities = responsibilities.clamp(min=1e-8)
			
			# ############################################### M-Step #################################################### #
			# M-Step: Update the model parameters i.e. latent prompt embeddings for each library
			#         by maximizing the likelihood of the data coming from the latent prompt of the library
			
			q_func = 0  # Total log-likelihood of the data coming from library, metric to track convergence
			responsibilities.to(args.device)
			
			# Library Book-keeping
			lib_train_logs = {}
			for k in range(args.num_libraries):
				
				# Likelihood of the sample coming from the latent prompt of library := p(x_n|z_k)
				resp_log_prob = get_response_log_probs(args, batch, tokenizer, model, k)
				
				# Re-normalise the responsibilities for library k -> Avoids numerical instability and does not affect EM
				norm_responsibilities = responsibilities[:, k] / responsibilities[:, k].sum()
				
				# Check norm_responsibilities are non-zero
				try:
					assert (norm_responsibilities != 0).all()
				except AssertionError:
					logger.info(
						f"Some responsibilities (after norm) for library {k} are still zero = {norm_responsibilities}")
				
				# Compute Loss = Negative Log Likelihood of the sample coming from the latent prompt of library k
				loss = -(resp_log_prob * norm_responsibilities.detach()).sum()
				
				# Compute the gradient norm
				# grad_norm = compute_grad_norm(model)
				
				# Update the model parameters
				loss.backward()
				optimizer.step()
				lr_scheduler.step()
				optimizer.zero_grad()
				
				# Update the total log-likelihood of the data coming from library
				q_func += -loss.detach().cpu().numpy().item()
				
				# Bookkeeping
				lib_train_logs[f"loss/lib_{k}"] = loss.detach().cpu().numpy().item()
			
			logger.info("Iteration: %d, Q-Func: %.4f", i, q_func)
			
			if args.wandb_logging:
				lib_train_logs.update({'q_func': q_func})
				wandb.log(lib_train_logs, step=i)
	
	# ################################################ Save Model ################################################## #
	# Save the model (by saving the trained embeddings for the latent prompt of each library)
	model = unwrap_model(model)
	logger.info("Saving the model at: %s", args.save_at)
	model.save_pretrained(save_directory=args.save_at)
	
	# ####################################### Compute final responsibilities ####################################### #
	# Debug by showing the responsibilities of each sample
	logger.info("\n\n# ################# Responsibilities ################# #")
	for i in tqdm(range(len(dataset)), desc="Computing Final Responsibilities", position=0, leave=True):
		batch = dataset.sample(i)
		batch = tuple(t.unsqueeze(0).to(args.device) for t in batch)
		
		responsibilities = compute_responsibilities(args, batch, tokenizer, model)
		# Debug by showing the responsibilities of each sample
		logger.info(f"[Responsibilities] {dataset.ids[i]}: {responsibilities.cpu().numpy()[0].tolist()}")


def main():
	args, logger = get_config()
	learn(args, logger)


if __name__ == '__main__':
	main()
