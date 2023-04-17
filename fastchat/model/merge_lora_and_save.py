"""
Apply the delta weights on top of a base model.

Usage:
python3 -m fastchat.model.apply_delta --base ~/model_weights/llama-13b --target ~/model_weights/vicuna-13b --delta lmsys/vicuna-13b-delta
"""
import argparse

import torch
from tqdm import tqdm
from transformers import AutoTokenizer, AutoModelForCausalLM
from peft import PeftModel


def merge_lora(base_model_path, target_model_path, lora_path):
    tokenizer = AutoTokenizer.from_pretrained(base_model_path)
    base_model = AutoModelForCausalLM.from_pretrained(
        base_model_path,
        torch_dtype=torch.float16,
        device_map='auto',
    )
    model = PeftModel.from_pretrained(
        base_model,
        lora_path,
        torch_dtype=torch.float16,
        device_map='auto'
    )
    # unwind broken decapoda-research config
    model.config.pad_token_id = tokenizer.pad_token_id = 0  # unk
    model.config.bos_token_id = 1
    model.config.eos_token_id = 2
    model = model.merge_and_unload()

    model.save_pretrained(target_model_path)
    tokenizer.save_pretrained(target_model_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-model-path", type=str, required=True)
    parser.add_argument("--target-model-path", type=str, required=True)
    parser.add_argument("--lora-path", type=str, required=True)

    args = parser.parse_args()

    merge_lora(args.base_model_path, args.target_model_path, args.lora_path)
