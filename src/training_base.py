import argparse
import torch
import os
import numpy as np
import pandas as pd
import pickle
import json
from data import prepare_dataset, prepare_dataset_exam
from datasets import concatenate_datasets
from peft import (
    LoraConfig,
    prepare_model_for_kbit_training,
    get_peft_model,
)
from transformers import (
    AutoTokenizer,
    AutoModelForCausalLM,
    BitsAndBytesConfig,
    TrainingArguments,
)
from trl import SFTTrainer
from constants import TRAINING_CONFIG_PATH

os.environ["WANDB_PROJECT"] = "ukrainian-finetuned-model"
os.environ["WANDB_LOG_MODEL"] = "checkpoint"

def main(args):

    train_dataset_pq = prepare_dataset(args['dataset_repo'], "prompt", "question").train_test_split(test_size=0.2)
    train_dataset_qr = prepare_dataset(args['dataset_repo'], "question", "response").train_test_split(test_size=0.2)

    smaller_train_dataset_pq = train_dataset_pq['test']
    smaller_train_dataset_qr = train_dataset_qr['test']

    train_dataset_pq_v2 = prepare_dataset(args['dataset_repo_v2'], "prompt", "question")
    train_dataset_qr_v2 = prepare_dataset(args['dataset_repo_v2'], "question", "response")

    train_dataset_exam = prepare_dataset_exam(args['exam_questions_repo'], "question", "answers", "correct_answers")
    
    train_dataset = concatenate_datasets([#smaller_train_dataset_pq, #smaller_train_dataset_qr, 
                                            train_dataset_pq_v2, train_dataset_qr_v2,
                                            train_dataset_exam])
                                            
    train_dataset = train_dataset.shuffle(seed=42)
    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_use_double_quant=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16,
    )

    model = AutoModelForCausalLM.from_pretrained(
        args['pretrained_ckpt'],
        quantization_config=bnb_config,
        use_cache=False,
        device_map="auto",
    )
    model.config.pretraining_tp = 1

    tokenizer = AutoTokenizer.from_pretrained(args['pretrained_ckpt'])
    tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "right"

    full_modules = ["q_proj",
                    "k_proj",
                    "v_proj",
                    "o_proj",
                    "gate_proj",
                    "up_proj",
                    "down_proj",
                    "lm_head"]

    peft_config = LoraConfig(
        lora_alpha=128,
        lora_dropout=args['dropout'],
        r=args['lora_r'],
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=full_modules 
    )

    model = prepare_model_for_kbit_training(model)
    model = get_peft_model(model, peft_config)

    training_args = TrainingArguments(
        output_dir=args['results_dir'],
        logging_dir=f"{args['results_dir']}/logs",
        num_train_epochs=args['epochs'],
        per_device_train_batch_size=10,
        gradient_accumulation_steps=2,
        gradient_checkpointing=True,
        optim="paged_adamw_32bit",
        logging_steps=100,
        learning_rate=2e-4,
        bf16=True,
        tf32=True,
        max_grad_norm=0.3,
        warmup_ratio=0.03,
        lr_scheduler_type="cosine",
        report_to="wandb"
    )

    max_seq_length = 2048 

    trainer = SFTTrainer(
        model=model,
        train_dataset=train_dataset,
        peft_config=peft_config,
        max_seq_length=max_seq_length,
        tokenizer=tokenizer,
        packing=True,
        args=training_args,
        dataset_text_field="instructions",
        neftune_noise_alpha=args['neftune']
    )

    trainer_stats = trainer.train()
    train_loss = trainer_stats.training_loss
    print(f"Training loss:{train_loss}")

    peft_model_id = f"{args['results_dir']}/assets"
    trainer.model.save_pretrained(peft_model_id)
    tokenizer.save_pretrained(peft_model_id)

    with open(f"{args['results_dir']}/results.pkl", "wb") as handle:
        run_result = [
            args['epochs'],
            args['lora_r'],
            args['dropout'],
            train_loss,
        ]
        pickle.dump(run_result, handle)
    print("Experiment over")
  
if __name__ == "__main__":
 
    with open(TRAINING_CONFIG_PATH, 'r') as config_file:
        args = json.load(config_file)
    
    main(args)