import os
import sys
import torch
import torch.nn as nn
import torch.nn.functional as F

import time
import logging
import wandb

from tqdm import tqdm
from typing import Any
from dataclasses import dataclass

from torch.utils.data import DataLoader
from transformers import Mistral3ForConditionalGeneration, AutoTokenizer
from peft import LoraConfig, LoraRuntimeConfig, get_peft_model

from .hf_utils import find_model_path
from .reward import RewardModel, RewardShaping
from .inference import InferenceModel
from .dataset import BaseDataset

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)


class EarlyStopping:
    def __init__(self, patience: int = 5, min_delta: float = 0.01, mode: str = "max"):
        self.patience = patience
        self.min_delta = min_delta
        self.mode = mode
        self.best_score = None
        self.wait = 0
        self.stopped_epoch = 0

    def __call__(self, current_score: float, epoch: int) -> bool:
        if self.best_score is None:
            self.best_score = current_score
            return False

        if self.mode == "max":
            if current_score > self.best_score + self.min_delta:
                self.best_score = current_score
                self.wait = 0
            else:
                self.wait += 1
        else:  # mode == "min"
            if current_score < self.best_score - self.min_delta:
                self.best_score = current_score
                self.wait = 0
            else:
                self.wait += 1

        if self.wait >= self.patience:
            self.stopped_epoch = epoch
            return True

        return False


@dataclass
class APEConfig:
    learning_rate: float = 1e-6
    batch_size: int = 16
    gradient_accumulation_steps: int = 4

    use_lora: bool = True
    lora_r: int = 16
    lora_alpha: int = 32
    lora_dropout: float = 0.1

    max_new_tokens: int = 512 

    max_epochs: int = 1
    save_freq: int = 100
    eval_freq: int = 50

    reward_lambda: float = 1.0
    entropy_weight: float = 0.01
    value_loss_weight: float = 0.5

    use_wandb: bool = False
    project_name: str = "ape-finetune"


class APETrainer:
    def __init__(
        self,
        config: APEConfig,
        inference_model: InferenceModel,
        train_dataset: BaseDataset,
        val_dataset: BaseDataset,
    ):
        self.model_name = "mistralai/Mistral-Small-3.1-24B-Instruct-2503"
        self.config = config
        self.inference_model = inference_model
        self.train_dataset = train_dataset
        self.val_dataset = val_dataset
        self.dtype = torch.bfloat16

        self._setup_models()
        self._setup_optimizer()

        self.early_stopping = EarlyStopping(
            patience=5,
            min_delta=0.01,
            mode="max",
        )

        if config.use_wandb:
            wandb.init(project=config.project_name)

    def _setup_models(self):
        _path = find_model_path(self.model_name)

        logging.info(f"Loading model: {self.model_name}...")
        start_time = time.time()

        self.tokenizer = AutoTokenizer.from_pretrained(
            _path,
            trust_remote_code=True,
        )

        self.tokenizer.pad_token = self.tokenizer.eos_token

        _base_model = Mistral3ForConditionalGeneration.from_pretrained(
            _path,
            attn_implementation="flash_attention_2",
            device_map="auto",
            torch_dtype=self.dtype,
        )


        if self.config.use_lora:
            self.lora_config = LoraConfig(
                target_modules=["q_proj", "v_proj"],
                use_dora=True,
                runtime_config=LoraRuntimeConfig(ephemeral_gpu_offload=True),
            )

            self.model = get_peft_model(_base_model, self.lora_config)
            self.hidden_size = 5120
        else:
            self.model = _base_model
            self.hidden_size = 4096
        
        
        self.device = self.model.device
        logging.info(f"Model loaded successfully in {(time.time() - start_time):.2f}s")

        self.value_head = nn.Linear(self.hidden_size, 1, dtype=self.dtype)
        self.value_head.to(self.model.device)

        self.reward_model = RewardModel(
            inference_model=self.inference_model,
            target_values=self.train_dataset.target_values,
        )

    def _setup_optimizer(self):
        _params = list(self.model.parameters()) + list(self.value_head.parameters())
        self.optimizer = torch.optim.AdamW(_params, lr=self.config.learning_rate)


    def generate(
        self,
        tokenized_inputs: Any,
        output_scores: bool = True,
        output_hidden_states: bool = False,
    ) -> tuple[Any, Any, Any]:
        generation_config = {
            "max_new_tokens": self.config.max_new_tokens,
            "do_sample": True,
            "temperature": 0.15,
            "pad_token_id": self.tokenizer.pad_token_id,
            "eos_token_id": self.tokenizer.eos_token_id,
        }

        input_length = tokenized_inputs["input_ids"].shape[-1]
        
        outputs = self.model.generate(
            **tokenized_inputs,
            **generation_config,
            return_dict_in_generate=True,
            output_scores=output_scores,
            output_hidden_states=output_hidden_states,
        )

        generated_tokens = outputs.sequences[:, input_length:]
        log_probs = None
        values = None

        if output_scores:
            log_probs = []

            for i, scores in enumerate(outputs.scores):
                if i < generated_tokens.shape[1]:
                    token_log_probs = F.log_softmax(scores, dim=-1)
                    selected_log_probs = token_log_probs.gather(
                        1, generated_tokens[:, i : i + 1]
                    )
                    log_probs.append(selected_log_probs)

            if log_probs:
                log_probs = torch.cat(log_probs, dim=1)
            else:
                print("WARNING: No log_probs collected, using zeros!")
                log_probs = torch.zeros_like(
                    generated_tokens, dtype=torch.float, device=self.device
                )

        if output_hidden_states:
            last_hidden_state = outputs.hidden_states[-1]
            last_token = last_hidden_state[-1]
            values = self.value_head(last_token).squeeze(-1)

        return generated_tokens, values, log_probs

    def compute_rewards(
        self,
        generated_texts: list[str],
        true_labels: list[str],
    ) -> tuple[torch.Tensor, tuple[str]]:
        rewards, y_pred = self.reward_model.evaluate(generated_texts, true_labels)
        rewards = rewards.to(dtype=self.dtype)
        return rewards, y_pred

    def train_step(self, data: tuple[str], labels: tuple[str]) -> dict[str, float]:
        self.model.train()
        self.value_head.train()

        tokenized_inputs = self.tokenizer(
            data,
            padding=True,
            return_tensors="pt",
        ).to(self.device)

        generated_tokens, values, log_probs = self.generate(
            tokenized_inputs,
            output_hidden_states=True,
        )

        generated_texts = self.tokenizer.batch_decode(
            generated_tokens, skip_special_tokens=True
        )

        rewards, _ = self.compute_rewards(generated_texts, labels)
        rewards = rewards.to(self.device)

        log_probs_clamped = torch.clamp(log_probs, min=-20, max=0)
        advantages = rewards - values.detach().squeeze(-1)
        policy_loss = -(log_probs_clamped.mean(dim=1) * advantages.detach()).mean()
        value_loss = F.mse_loss(values.squeeze(-1), rewards)

        probs = log_probs_clamped.exp()
        entropy = -(probs * log_probs_clamped).mean()
        entropy_loss = -self.config.entropy_weight * entropy

        total_loss = (
            policy_loss + self.config.value_loss_weight * value_loss + entropy_loss
        )

        self.optimizer.zero_grad()
        total_loss.backward()
        torch.nn.utils.clip_grad_norm_(
            list(self.model.parameters()) + list(self.value_head.parameters()),
            max_norm=1.0,
        )
        self.optimizer.step()

        return {
            "total_loss": total_loss.item(),
            "policy_loss": policy_loss.item(),
            "value_loss": value_loss.item(),
            "entropy_loss": entropy_loss.item(),
            "mean_reward": rewards.mean().item(),
            "mean_advantage": advantages.mean().item(),
        }

    def train(self, use_tqdm: bool = True):
        logging.info("Starting training...")

        dataloader = DataLoader(
            self.train_dataset,
            batch_size=self.config.batch_size,
            shuffle=True,
        )

        total_steps = 0
        should_stop = False

        for epoch in range(self.config.max_epochs):
            if should_stop:
                logging.info(f"Early stopping triggered at epoch {epoch}")
                break

            logging.info(f"Epoch {epoch + 1}/{self.config.max_epochs}")

            epoch_stats = {
                "total_loss": 0.0,
                "policy_loss": 0.0,
                "value_loss": 0.0,
                "entropy_loss": 0.0,
                "mean_reward": 0.0,
            }

            for data, labels in tqdm(dataloader, disable=not use_tqdm):
                stats = self.train_step(data, labels)
                total_steps += 1

                for key in epoch_stats:
                    if key in stats:
                        epoch_stats[key] += stats[key]

                break

                # if total_steps % 10 == 0:
                #     logging.info(
                #         f"Step {total_steps}: Loss = {stats['total_loss']:.4f}, "
                #         f"Reward = {stats['mean_reward']:.4f}"
                #     )

                # if self.config.use_wandb:
                #     wandb.log(
                #         {f"train/{k}": v for k, v in stats.items()}, step=total_steps
                #     )

                # if total_steps % self.config.eval_freq == 0:
                #     val_stats = self.evaluate()

                #     if self.config.use_wandb:
                #         wandb.log(
                #             {f"val/{k}": v for k, v in val_stats.items()},
                #             step=total_steps,
                #         )

                #     should_stop = self.early_stopping(val_stats["mean_reward"], epoch)

                # if total_steps % self.config.save_freq == 0:
                #     self.save_model(f"checkpoint-{total_steps}")

            num_batches = len(dataloader)
            avg_stats = {k: v / num_batches for k, v in epoch_stats.items()}
            logging.info(f"Epoch {epoch + 1} completed: {avg_stats}")

        # self.save_model("final_model")
        logging.info("Training completed!")

    def evaluate(self, use_tqdm: bool = False):
        self.model.eval()
        self.value_head.eval()

        dataloader = DataLoader(
            self.val_dataset,
            batch_size=self.config.batch_size,
            shuffle=False,
        )

        all_predictions = []
        all_labels = []

        with torch.no_grad():
            for data, labels in tqdm(dataloader, disable=not use_tqdm):
                X = self.tokenizer(
                    data,
                    padding=True,
                    return_tensors="pt",
                ).to(self.device)

                generated_tokens, _, _ = self.generate(X, output_scores=False)
                generated_texts = self.tokenizer.batch_decode(
                    generated_tokens, skip_special_tokens=True
                )

                _, preds = self.compute_rewards(generated_texts, labels)
                all_predictions.extend(preds)
                all_labels.extend(labels)

        self.model.train()
        self.value_head.train()

        return all_predictions, all_labels


    def save_model(self, checkpoint_name: str):
        save_path = f"./checkpoints/{checkpoint_name}"
        os.makedirs(save_path, exist_ok=True)

        self.model.save_pretrained(save_path)

        torch.save(
            self.value_head.state_dict(), 
            os.path.join(save_path, "value_head.pt"),
        )

        logging.info(f"Model saved to {save_path}")

    # def load_model(self, checkpoint_path: str):
    #     """모델 로드"""
    #     logging.info(f"Loading model from {checkpoint_path}")

    #     # PEFT 모델 로드
    #     self.model.load_adapter(checkpoint_path)

    #     # Value head 로드
    #     value_head_path = os.path.join(checkpoint_path, "value_head.pt")
    #     if os.path.exists(value_head_path):
    #         self.value_head.load_state_dict(torch.load(value_head_path))

    #     logging.info("Model loaded successfully")
