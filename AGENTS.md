# Repository Guidelines

## Project Structure & Module Organization

This repository is a dependency-free GitHub Pages game with Python tooling for
the Million Doubt rules engine and policy model.

- `index.html`, `styles.css`, and `app.js` contain the browser UI and game loop.
- `train_milliondoubt.py` contains the simulator, observation/action encoding,
  and self-play training baseline.
- `export_model_for_web.py` converts a PyTorch checkpoint into `model.json` for
  browser inference.
- `RULEBOOK.md` documents the researched rules; `NN_TRAINING.md` documents the
  training workflow and known limitations.
- `.github/workflows/pages.yml` deploys the repository root to GitHub Pages.
- Generated checkpoints belong in `checkpoints/` and are ignored by Git. Keep
  only the intentionally published web model, `model.json`, in the repository.

## Build, Test, and Development Commands

There is no compile step or package manager. Run the web app with:

```bash
python3 -m http.server 4173
```

Open `http://127.0.0.1:4173/` to verify model loading and gameplay. Run the
Python rule-engine smoke test with:

```bash
python3 train_milliondoubt.py --smoke
```

For a short training check, use `.venv/bin/python train_milliondoubt.py
--episodes 64 --batch-episodes 8 --device cpu`. Export a checkpoint with
`.venv/bin/python export_model_for_web.py checkpoints/milliondoubt_policy.pt
--out model.json`.

## Coding Style & Naming Conventions

Use four-space indentation in Python and two-space indentation in JavaScript
and CSS. Prefer readable, small functions and explicit state transitions.
Use `snake_case` for Python names, `camelCase` for JavaScript names, and
lowercase hyphenated filenames. Keep browser code dependency-free unless the
deployment workflow is updated accordingly.

## Testing Guidelines

The smoke test is the required automated check for Python changes. Also run
`python3 -m py_compile train_milliondoubt.py export_model_for_web.py` and
`node --check app.js` when touching those files. Manually test a new browser
flow through a local HTTP server, including model fallback/loading when relevant.

## Commit & Pull Request Guidelines

Use concise imperative commit subjects with a category, matching the existing
history (for example, `feat: add ...` or `chore: refresh ...`). Pull requests
should describe gameplay or training behavior changes, list commands run, and
include a screenshot or deployed URL for visible UI changes. Keep generated
training logs and local checkpoints out of commits.

## Security & Configuration Tips

Do not commit credentials, private hand data, or machine-specific paths. The
browser model must receive only public game state and the player's own hand;
preserve that information boundary when changing observations or inference.
