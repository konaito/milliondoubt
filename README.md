# Million Doubt

ミリオンダウトをブラウザで遊べる、GitHub Pages対応の静的アプリです。ルールベースCPUに加えて、自己対戦で学習したWeb NNモデルとも対戦できます。

## 公開アプリ

<https://konaito.github.io/milliondoubt/>

## ローカル開発

ビルドツールやNode.jsは使いません。HTTPサーバーを起動してから、ブラウザで <http://127.0.0.1:4173/> を開いてください。

```bash
python3 -m http.server 4173
```

`index.html` を直接開くこともできますが、`model.json` の読み込み確認にはHTTPサーバーを使ってください。

## 検証

Pythonのルールエンジンとモデル変換スクリプトを変更した場合は、次を実行します。

```bash
python3 train_milliondoubt.py --smoke
python3 -m py_compile train_milliondoubt.py export_model_for_web.py
node --check app.js
```

ゲームルールやUIを変更した場合は、ローカルサーバー上で実際にゲームを開始し、カード出し・パス・ダウト・CPU動作を確認してください。

## 対戦の所作

公式掲載ルールに合わせて、親は毎局ランダムに決まり、カードを出す・パス・ダウト・スルー・ペナルティ確定の後は持ち時間に10秒を加算します。全裏札のセットは場札として残しながら、次の比較対象からは除外します。ダウト時は裏札を公開してから、任意枚数のペナルティ札を選び、成功側または失敗側を次の親にします。

## NNモデル

学習方法と観測情報の設計は [NN_TRAINING.md](NN_TRAINING.md) にまとめています。PyTorchチェックポイントをブラウザ用に変換するには、学習環境で次を実行します。

```bash
.venv/bin/python export_model_for_web.py \
  checkpoints/milliondoubt_policy.pt --out model.json
```

`model.json` がルートにあると、対戦画面がWeb NNを自動で読み込みます。ファイルが無い場合はルールベースCPUにフォールバックします。チェックポイント、学習ログ、仮想環境はGit管理しません。

## GitHub Pages

`.github/workflows/pages.yml` が `main` へのpushごとにリポジトリのルートを公開します。GitHubのSettings → Pagesで、SourceをGitHub Actionsに設定してください。

ルールの調査内容は [RULEBOOK.md](RULEBOOK.md) を参照してください。貢献時の構成・命名・Commit方針は [AGENTS.md](AGENTS.md) に記載しています。
