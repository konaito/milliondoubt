# Million Doubt

ミリオンダウトをブラウザで遊ぶための、GitHub Pages対応の静的アプリです。

## ローカルで遊ぶ

ビルドツールは使っていません。次のコマンドで起動できます。

```bash
python3 -m http.server 4173
```

ブラウザで <http://127.0.0.1:4173/> を開きます。`index.html` を直接開くこともできますが、学習済みモデルの読み込み確認にはHTTPサーバーを使ってください。

## GitHub Pages

このディレクトリをGitHubリポジトリのルートに置き、Settings → Pages → SourceでGitHub Actionsを選べば、同梱の `.github/workflows/pages.yml` が `main` へのpushごとに公開します。ビルドやNode.jsは不要です。Actionsを使わない場合は、Settings → Pages → Deploy from a branch → `main` / `/ (root)` でも公開できます。

## NNモデル

モデルが無い場合はルールベースCPUで対戦できます。PyTorchチェックポイントをWeb用に変換する場合は、学習環境で次を実行します。

```bash
.venv/bin/python export_model_for_web.py checkpoints/milliondoubt_policy.pt --out model.json
```

生成した `model.json` をこのディレクトリのルートに置くと、対戦画面がWeb NNを自動で読み込みます。`model.json` は大きくなるため、GitHub Pagesへ公開するリポジトリに含める場合はサイズを確認してください。

ルールの調査内容は [RULEBOOK.md](RULEBOOK.md)、NNの学習方法は [NN_TRAINING.md](NN_TRAINING.md) にまとめています。
