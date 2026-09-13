#!/usr/bin/env python3
"""Export a Million Doubt PyTorch checkpoint to the static web app format."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List

import torch


def values(tensor: torch.Tensor) -> List[Any]:
    return tensor.detach().cpu().tolist()


def export(checkpoint_path: Path, output_path: Path) -> None:
    checkpoint = torch.load(checkpoint_path, map_location="cpu")
    state = checkpoint["model"]
    layers: List[Dict[str, Any]] = []
    for index in (0, 3, 6):
        layers.append({
            "type": "linear",
            "weight": values(state[f"trunk.{index}.weight"]),
            "bias": values(state[f"trunk.{index}.bias"]),
        })
        if index != 6:
            norm_index = index + 1
            layers.append({
                "type": "layernorm",
                "weight": values(state[f"trunk.{norm_index}.weight"]),
                "bias": values(state[f"trunk.{norm_index}.bias"]),
                "eps": 1e-5,
            })
            layers.append({"type": "gelu"})
        else:
            layers.append({"type": "gelu"})
    layers.append({
        "type": "linear",
        "weight": values(state["logit.weight"]),
        "bias": values(state["logit.bias"]),
    })

    payload = {
        "format": "milliondoubt-web-v1",
        "source": checkpoint_path.name,
        "episodes": checkpoint.get("episodes"),
        "strategy": checkpoint.get("strategy", "legacy-self-play"),
        "obs_size": checkpoint.get("obs_size", 194),
        "action_size": checkpoint.get("action_size", 186),
        "layers": layers,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    print(json.dumps({"output": str(output_path), "bytes": output_path.stat().st_size, "episodes": payload["episodes"]}, ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser(description="Export the Million Doubt policy for GitHub Pages")
    parser.add_argument("checkpoint", type=Path)
    parser.add_argument("--out", type=Path, default=Path("model.json"))
    args = parser.parse_args()
    export(args.checkpoint, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
