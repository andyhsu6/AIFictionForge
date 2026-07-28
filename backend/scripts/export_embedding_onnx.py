"""Export the current Sentence Transformer checkpoint to a minimal FP32 ONNX bundle."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import torch
from torch import nn
from transformers import AutoModel, AutoTokenizer


MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"
BACKEND_DIR = Path(__file__).resolve().parents[1]
MODEL_CACHE_DIR = BACKEND_DIR / "embedding" / f"models--sentence-transformers--{MODEL_NAME}"


class LastHiddenStateModel(nn.Module):
    def __init__(self, model: nn.Module) -> None:
        super().__init__()
        self.model = model

    def forward(
        self,
        input_ids: torch.Tensor,
        attention_mask: torch.Tensor,
        token_type_ids: torch.Tensor,
    ) -> torch.Tensor:
        return self.model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            return_dict=True,
        ).last_hidden_state


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    revision = (MODEL_CACHE_DIR / "refs" / "main").read_text(encoding="utf-8").strip()
    parser.add_argument(
        "--source",
        type=Path,
        default=MODEL_CACHE_DIR / "snapshots" / revision,
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=BACKEND_DIR / "embedding" / "onnx" / MODEL_NAME,
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    source = args.source.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)

    sentence_config = json.loads(
        (source / "sentence_bert_config.json").read_text(encoding="utf-8")
    )
    pooling_config = json.loads(
        (source / "1_Pooling" / "config.json").read_text(encoding="utf-8")
    )
    if not pooling_config.get("pooling_mode_mean_tokens"):
        raise ValueError("导出脚本仅支持 Mean Pooling 模型")

    tokenizer = AutoTokenizer.from_pretrained(source, local_files_only=True)
    model = AutoModel.from_pretrained(source, local_files_only=True).eval()
    wrapped_model = LastHiddenStateModel(model).eval()
    sample = tokenizer(
        ["用于验证动态批次和序列长度的示例文本。"],
        padding=True,
        truncation=True,
        max_length=int(sentence_config["max_seq_length"]),
        return_tensors="pt",
    )

    torch.onnx.export(
        wrapped_model,
        (
            sample["input_ids"],
            sample["attention_mask"],
            sample["token_type_ids"],
        ),
        output / "model.onnx",
        input_names=["input_ids", "attention_mask", "token_type_ids"],
        output_names=["last_hidden_state"],
        dynamic_axes={
            "input_ids": {0: "batch", 1: "sequence"},
            "attention_mask": {0: "batch", 1: "sequence"},
            "token_type_ids": {0: "batch", 1: "sequence"},
            "last_hidden_state": {0: "batch", 1: "sequence"},
        },
        opset_version=17,
        dynamo=False,
    )

    shutil.copy2(source / "tokenizer.json", output / "tokenizer.json")
    config = {
        "model_name": MODEL_NAME,
        "max_seq_length": int(sentence_config["max_seq_length"]),
        "embedding_dimension": int(pooling_config["word_embedding_dimension"]),
        "pooling": "mean",
        "normalize": False,
        "pad_token": tokenizer.pad_token,
    }
    (output / "embedding_config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"ONNX model exported to {output}")


if __name__ == "__main__":
    main()
