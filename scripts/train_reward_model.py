import os
import sys

from transformers import (AutoTokenizer, Trainer, TrainingArguments,
                          default_data_collator)

sys.path.append('../')
from chatgpt.dataset.comparison_dataset import PairwiseDataset
from chatgpt.rlhf.reward_model import RewardModel


def compute_metrics(eval_preds):
    chosen_reward = eval_preds.predictions[1]  # chosen scores
    rejected_reward = eval_preds.predictions[2]  # rejected scores

    result = {}
    acc = sum(chosen_reward > rejected_reward) / len(chosen_reward)
    result["accuracy"] = acc

    return result


if __name__ == '__main__':
    tokenizer = AutoTokenizer.from_pretrained('facebook/opt-125m')
    tokenizer.pad_token = tokenizer.eos_token

    if not os.path.exists('rm_checkpoint'):
        os.mkdir('rm_checkpoint')

    training_args = TrainingArguments(
        output_dir='rm_checkpoint/',
        num_train_epochs=5,
        logging_steps=10,
        gradient_accumulation_steps=4,
        save_strategy='steps',
        evaluation_strategy='steps',
        per_device_train_batch_size=8,
        per_device_eval_batch_size=8,
        eval_accumulation_steps=1,
        eval_steps=500,
        save_steps=500,
        warmup_steps=100,
        fp16=True,
        learning_rate=1e-5,
        save_total_limit=5,
        logging_dir='./logs',
    )

    # Initialize the reward model from the (supervised) fine-tuned GPT-J
    model = RewardModel(model='opt', pretrained='facebook/opt-125m')
    # Create the comparisons datasets
    data_path = 'CarperAI/openai_summarize_comparisons'
    # Make pairwise datasets for training
    train_dataset = PairwiseDataset(data_path,
                                    tokenizer,
                                    split='train',
                                    max_length=512)
    val_dataset = PairwiseDataset(data_path,
                                  tokenizer,
                                  split='valid1',
                                  max_length=512)

    trainer = Trainer(model=model,
                      args=training_args,
                      train_dataset=train_dataset,
                      eval_dataset=val_dataset,
                      data_collator=default_data_collator)
    trainer.train()
