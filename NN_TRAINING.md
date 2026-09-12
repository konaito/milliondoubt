# ミリオンダウト NN 学習

`RULEBOOK.md` を基準に、対戦相手の手札を観測へ漏らさない自己対戦学習の土台です。

このモデルは、次の3種類の判断を同じネットワークで学習します。

- カードを表裏指定付きで出す、またはパスする
- 裏札に対してダウトするかスルーする
- ダウト解決後に、場札のどれを相手へ渡すか選ぶ

観測には自分の実手札と、場の表札・裏札枚数・公開された状態だけを入れます。相手の非公開手札、裏札の実物、未使用カードは入力しません。

## 実行

まずルールエンジンのスモークテストを実行します。

```bash
python3 train_milliondoubt.py --smoke
```

Incertotech Macでは、Python 3.9の仮想環境を作って依存関係を入れます。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
.venv/bin/python train_milliondoubt.py --episodes 10000 --device auto
```

Apple Silicon版PyTorchでMPSが使える場合は、`--device auto` がMPSを選択します。チェックポイントは既定で `checkpoints/milliondoubt_policy.pt` に保存されます。

短い動作確認だけなら次のコマンドで十分です。

```bash
.venv/bin/python train_milliondoubt.py --episodes 64 --batch-episodes 8 --device cpu
```

## 強くするための運用

自己対戦だけを長時間回すと、同じネットワークの癖に過適合します。実戦投入する前に、少なくとも次を追加します。

1. 過去チェックポイントを固定した対戦相手プールに入れる。
2. 人間の棋譜を公開情報だけで再生し、行動模倣のデータとして混ぜる。
3. ルールが確定していない全裏札、同時特殊効果、最終手ダウトを実ゲームで観測してテストにする。
4. 乱数シードを変えた複数モデルと、古いチェックポイントに対して勝ち枚数で評価する。
5. NNの一手選択に、公開状態からのサンプリング付き探索を加える。

このスクリプトは学習を開始できる土台です。「最強」を名乗れるかは、学習量、棋譜量、実ゲーム仕様との一致、チェックポイント対戦で継続的に検証します。
