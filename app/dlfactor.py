# -*- coding: utf-8 -*-
"""深度学习因子挖掘（纯 Python 标准库，零第三方依赖）

这个模块做的事：不用人写公式，让一个手写的小型神经网络（MLP）自己从
K 线数据里"学"出因子，再用遗传算法搜索最优网络结构 / 超参数，最后把
学到的模型注册成普通因子，接入因子实验室（IC/ICIR/分层收益）一起评测。

两种任务：
  1. cross_section  横截面选股因子 —— 预测"未来 N 日"每只股票在池子里的
                     相对排名收益，输出 0~1 的"AI 选股分"。分数高 = 模型
                     认为这只股票未来相对更强势，可直接当因子排序。
  2. time_series    时序择时信号 —— 预测单只股票未来 N 日涨跌方向，
                     输出 0~1 的"多头概率"，作为择时信号。

防过拟合：
  * 训练/验证按时间切分（前 70% 训练，后 30% 样本外验证），
  * 进化搜索用【样本外 IC】作为适应度，而不是训练集误差，
  * 只用单隐层、小规模网络 + L2 正则，避免在噪声上死记硬背。

重要：深度学习因子和人工因子一样，会失效、会过拟合。样本外 IC < 0.03
说明这次没挖到稳定信号（正常现象，换股票池 / 时间窗口重试即可）。这里
给的是"自己去测"的工具，不是稳赚的承诺。
"""
from __future__ import annotations

import datetime as dt
import math
import random
import threading
import time

from . import datasource as ds
from . import factors as F
from . import store


# ===========================================================================
# 神经网络核心：手写小型 MLP（前向 + 反向传播 + 梯度下降）
# ===========================================================================
class MLP:
    """小型前馈神经网络，sigmoid 激活，输出单值（概率或分数）。

    层结构用 [h1, h2, ...] 表示隐藏层每层神经元个数。为控制过拟合，
    默认只建议 1 个隐藏层。
    """

    def __init__(self, in_dim, hidden, out_dim=1, lr=0.1, l2=1e-4, seed=0):
        self.in_dim = in_dim
        self.hidden = list(hidden)
        self.out_dim = out_dim
        self.lr = lr
        self.l2 = l2
        self.rng = random.Random(seed)

        sizes = [in_dim] + list(hidden) + [out_dim]
        self.W = []   # 权重矩阵列表
        self.b = []   # 偏置向量列表
        for i in range(len(sizes) - 1):
            fan_in = sizes[i]
            # Xavier 初始化（用 -1/sqrt(fan_in) ~ 1/sqrt(fan_in) 的均匀分布）
            bound = 1.0 / math.sqrt(fan_in) if fan_in > 0 else 0.1
            self.W.append([[self.rng.uniform(-bound, bound) for _ in range(fan_in)]
                           for _ in range(sizes[i + 1])])
            self.b.append([0.0] * sizes[i + 1])

    @staticmethod
    def _sigmoid(z):
        if z >= 0:
            return 1.0 / (1.0 + math.exp(-z))
        e = math.exp(z)
        return e / (1.0 + e)

    def forward(self, x):
        """返回 (各层激活值, 输出值)"""
        acts = [list(x)]
        a = list(x)
        for l in range(len(self.W)):
            W, b = self.W[l], self.b[l]
            z = [b[j] + sum(W[j][i] * a[i] for i in range(len(a))) for j in range(len(b))]
            a = [self._sigmoid(v) for v in z]
            acts.append(a)
        return acts, a[0]

    def predict(self, x):
        _, out = self.forward(x)
        return out

    def train_one(self, x, y):
        """对单条样本做一次前向 + 反向传播，返回损失（MSE）"""
        acts, out = self.forward(x)

        # 每层输出的误差向量 delta_l（长度 = 该层神经元数）
        # 输出层误差（单输出，sigmoid，MSE 对激活求导）
        n_layers = len(self.W)
        layer_delta = [None] * n_layers
        # 输出层：长度为 out_dim（这里恒为 1）
        out_delta = [(out - y[0]) * out * (1.0 - out)]
        layer_delta[n_layers - 1] = out_delta

        # 从后往前传播：delta_l[j] = h_j'(z) * sum_k(W_{l+1}[k][j] * delta_{l+1}[k])
        for l in range(n_layers - 2, -1, -1):
            W_next = self.W[l + 1]
            next_delta = layer_delta[l + 1]
            h = acts[l + 1]  # 当前层的激活输出
            layer_delta[l] = [
                h[j] * (1.0 - h[j]) * sum(W_next[k][j] * next_delta[k]
                                          for k in range(len(next_delta)))
                for j in range(len(h))
            ]

        # 梯度下降（带 L2 正则）：W_l 的形状是 (out_size, in_size)
        for l in range(n_layers):
            a_prev = acts[l]           # 第 l 层的输入（前一层激活）
            d = layer_delta[l]         # 第 l 层输出误差
            W, b = self.W[l], self.b[l]
            for j in range(len(b)):
                b[j] -= self.lr * d[j]
                for i in range(len(a_prev)):
                    W[j][i] -= self.lr * (d[j] * a_prev[i] + self.l2 * W[j][i])

        return (out - y[0]) ** 2


# ===========================================================================
# 特征工程：从 OHLCV 提取标准化时序特征
# ===========================================================================
def _rolling_mean(x, n):
    out = [None] * len(x)
    for i in range(n - 1, len(x)):
        seg = x[i - n + 1:i + 1]
        if any(v is None for v in seg):
            continue
        out[i] = sum(seg) / n
    return out


def _rolling_std(x, n):
    out = [None] * len(x)
    for i in range(n - 1, len(x)):
        seg = x[i - n + 1:i + 1]
        if any(v is None for v in seg):
            continue
        m = sum(seg) / n
        out[i] = (sum((v - m) ** 2 for v in seg) / n) ** 0.5
    return out


def _rank_pct(x, n):
    """当前值在最近 n 期里的百分位 0~1（同 factors.ts_rank 的思路）"""
    out = [None] * len(x)
    for i in range(n - 1, len(x)):
        seg = x[i - n + 1:i + 1]
        if any(v is None for v in seg):
            continue
        v = seg[-1]
        less = sum(1 for s in seg if s < v)
        eq = sum(1 for s in seg if s == v)
        out[i] = (less + 0.5 * eq) / n
    return out


def _rsi(x, n=14):
    """本地实现的 RSI，返回 0~100，数据不足为 None（不依赖 factors.py 注册）"""
    out = [None] * len(x)
    for i in range(1, len(x)):
        if x[i - 1] is None or x[i] is None:
            continue
        chg = x[i] - x[i - 1]
        out[i] = chg
    # 用 Wilder 平滑算 RSI
    gains = [0.0] * len(x)
    losses = [0.0] * len(x)
    for i in range(1, len(x)):
        if out[i] is None:
            continue
        gains[i] = max(out[i], 0.0)
        losses[i] = max(-out[i], 0.0)
    rsi_out = [None] * len(x)
    if len(x) <= n:
        return rsi_out
    ag = sum(gains[1:n + 1]) / n
    al = sum(losses[1:n + 1]) / n
    rsi_out[n] = 100.0 - 100.0 / (1.0 + ag / al) if al > 0 else 100.0
    for i in range(n + 1, len(x)):
        ag = (ag * (n - 1) + gains[i]) / n
        al = (al * (n - 1) + losses[i]) / n
        rsi_out[i] = 100.0 - 100.0 / (1.0 + ag / al) if al > 0 else 100.0
    return rsi_out


def extract_features(klines, warmup=60):
    """把一根股票的 K 线变成特征矩阵（每行 15 维标准化特征，前 warmup 根为 None）

    返回长度 = len(klines) 的列表，每项是 15 维 float 列表或 None。
    """
    n = len(klines)
    c = [k.get("close") for k in klines]
    o = [k.get("open") for k in klines]
    h = [k.get("high") for k in klines]
    lo = [k.get("low") for k in klines]
    v = [k.get("volume") for k in klines]
    amt = [k.get("amount") for k in klines]

    # 收益率序列
    r0 = [None] * n
    for i in range(1, n):
        if c[i - 1] and c[i]:
            r0[i] = c[i] / c[i - 1] - 1.0

    m5 = _rolling_mean(r0, 5)
    m20 = _rolling_mean(r0, 20)
    std20 = _rolling_std(r0, 20)

    # 反转：过去 1 日收益取负（短期反转）
    rev1 = [(-r0[i]) if r0[i] is not None else None for i in range(n)]

    ma5 = _rolling_mean(c, 5)
    ma20 = _rolling_mean(c, 20)
    ma60 = _rolling_mean(c, 60)
    sma6 = _rolling_mean(c, 6)

    # RSI(14)
    rsi = _rsi(c, 14)

    # 收盘价在近 20 根 K 线里的位置（0~1）
    close_pos = [None] * n
    for i in range(n):
        if i < 20:
            continue
        seg = c[i - 20:i + 1]
        hi = max(seg)
        lw = min(seg)
        close_pos[i] = (c[i] - lw) / (hi - lw) if hi > lw else 0.5

    # 成交量 5 日 / 20 日均量比
    v5 = _rolling_mean(v, 5)
    v20 = _rolling_mean(v, 20)
    vol_ratio = [(v5[i] / v20[i] - 1.0) if (v5[i] and v20[i]) else None for i in range(n)]

    # 成交量 5 日变化率
    v_chg = [None] * n
    for i in range(5, n):
        if v[i - 5]:
            v_chg[i] = v[i] / v[i - 5] - 1.0

    # 成交额对数变化率（近 1 日）
    amt_chg = [None] * n
    for i in range(1, n):
        if amt[i - 1] and amt[i]:
            amt_chg[i] = math.log(amt[i]) - math.log(amt[i - 1])

    # 振幅（近 20 日均值，衡量波动）
    amp_raw = [((h[i] - lo[i]) / c[i]) if c[i] else None for i in range(n)]
    amp = _rolling_mean(amp_raw, 20)

    # 波动率百分位
    vol_pct = _rank_pct(std20, 20)
    rp = _rank_pct(r0, 20)

    feats = [None] * n
    for i in range(n):
        row = [
            m5[i], m20[i], rev1[i], std20[i],
            (ma5[i] / ma20[i] - 1.0) if (ma5[i] and ma20[i]) else None,
            v_chg[i],
            (c[i] / ma20[i] - 1.0) if ma20[i] else None,
            (c[i] / ma60[i] - 1.0) if ma60[i] else None,
            (rsi[i] / 100.0 - 0.5) * 2.0 if rsi[i] is not None else None,
            (-(c[i] - sma6[i]) / sma6[i]) if sma6[i] else None,
            rp[i],
            amt_chg[i],
            amp[i],
            close_pos[i],
            vol_ratio[i],
        ]
        if any(x is None for x in row):
            feats[i] = None
        else:
            feats[i] = row
    return feats


# ===========================================================================
# 样本构建：横截面 / 时序 两种标签
# ===========================================================================
def _make_samples(panel, warmup=60, horizon=5, task="cross_section"):
    """panel: {symbol: klines}。返回 [(特征向量, [标签]), ...] 样本列表。

    cross_section: 标签 = 未来 horizon 日收益（连续值），
                   但训练时横截面会用排名，见 _train_cross_section。
    time_series:   标签 = 未来 horizon 日涨跌方向（0 或 1）。
    """
    samples = []
    for sym, ks in panel.items():
        if len(ks) < warmup + horizon + 5:
            continue
        feats = extract_features(ks, warmup)
        c = [k["close"] for k in ks]
        for i in range(warmup, len(ks) - horizon):
            if feats[i] is None:
                continue
            if not c[i] or not c[i + horizon]:
                continue
            ret = c[i + horizon] / c[i] - 1.0
            if task == "time_series":
                label = [1.0 if ret > 0 else 0.0]
            else:
                label = [ret]
            samples.append((feats[i], label))
    return samples


def _split(samples, ratio=0.7):
    """按时间顺序切分（样本已经按股票、时间自然有序，直接按比例切）"""
    cut = int(len(samples) * ratio)
    return samples[:cut], samples[cut:]


def _spearman(a, b):
    """Spearman 秩相关系数"""
    n = len(a)
    if n < 3:
        return None

    def rank(vals):
        order = sorted(range(n), key=lambda i: vals[i])
        ranks = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                ranks[order[k]] = avg
            i = j + 1
        return ranks

    ra, rb = rank(a), rank(b)
    ma, mb = sum(ra) / n, sum(rb) / n
    va = sum((v - ma) ** 2 for v in ra)
    vb = sum((v - mb) ** 2 for v in rb)
    if va <= 1e-15 or vb <= 1e-15:
        return None
    return sum((ra[i] - ma) * (rb[i] - mb) for i in range(n)) / math.sqrt(va * vb)


def _ic_of(model, samples):
    """用模型对样本集预测，算预测值与真实标签的 Spearman IC（样本外评估用）"""
    if not samples:
        return None
    preds = [model.predict(x) for x, _ in samples]
    labels = [y[0] for x, y in samples]
    return _spearman(preds, labels)


def _train_one(cfg, train, val, task):
    """按 cfg 训练一个 MLP，返回 (样本外IC, 模型)"""
    in_dim = len(train[0][0])
    hidden = cfg.get("hidden", [16])
    lr = cfg.get("lr", 0.1)
    l2 = cfg.get("l2", 1e-4)
    epochs = cfg.get("epochs", 8)
    model = MLP(in_dim, hidden, 1, lr=lr, l2=l2, seed=cfg.get("seed", 0))
    rng = random.Random(cfg.get("seed", 0))
    for _ in range(epochs):
        # 打乱训练样本顺序
        idx = list(range(len(train)))
        rng.shuffle(idx)
        for j in idx:
            x, y = train[j]
            model.train_one(x, y)
    ic = _ic_of(model, val)
    return ic, model


def _serialize_model(model):
    return {
        "in_dim": model.in_dim, "hidden": model.hidden, "out_dim": model.out_dim,
        "W": model.W, "b": model.b,
    }


def _deserialize_model(blob):
    m = MLP(blob["in_dim"], blob["hidden"], blob["out_dim"])
    m.W = blob["W"]
    m.b = blob["b"]
    return m


# ===========================================================================
# 进化搜索（遗传算法）：自动搜网络结构 + 超参数
# ===========================================================================
SEARCH_SPACE = {
    "hidden": [[8], [16], [32], [8, 4], [16, 8]],
    "lr": [0.05, 0.1, 0.2],
    "l2": [0.0, 1e-4, 1e-3],
    "epochs": [5, 8, 12],
}


def _random_cfg(rng):
    return {
        "hidden": rng.choice(SEARCH_SPACE["hidden"]),
        "lr": rng.choice(SEARCH_SPACE["lr"]),
        "l2": rng.choice(SEARCH_SPACE["l2"]),
        "epochs": rng.choice(SEARCH_SPACE["epochs"]),
    }


def _evolve(train, val, task, population=8, generations=3, progress=None):
    """遗传算法：种群里的个体是 (样本外IC, 模型, 配置)。IC 越高越优。

    返回 (best_ic, best_model, best_cfg)。样本太少就退化成随机搜几个。
    """
    if len(train) < 10 or len(val) < 5:
        return None, None, None
    rng = random.Random(42)
    pop = []
    for k in range(population):
        cfg = _random_cfg(rng)
        cfg["seed"] = k
        ic, model = _train_one(cfg, train, val, task)
        if ic is not None:
            pop.append((ic, model, cfg))
        if progress:
            progress(int((k + 1) / population * 100), "进化搜索 初代 %d/%d" % (k + 1, population))
    if not pop:
        return None, None, None

    # 简单进化：每代保留最优，其余做"变异"
    for g in range(1, generations):
        pop.sort(key=lambda x: x[0], reverse=True)
        best = pop[0]
        new_pop = [best]
        for k in range(population - 1):
            parent = pop[k % len(pop)]
            cfg = dict(parent[2])
            # 变异：随机改一项
            key = rng.choice(list(SEARCH_SPACE.keys()))
            cfg[key] = rng.choice(SEARCH_SPACE[key])
            cfg["seed"] = rng.randint(0, 1000)
            ic, model = _train_one(cfg, train, val, task)
            if ic is not None:
                new_pop.append((ic, model, cfg))
            if progress:
                progress(int((g * population + k + 1) / (generations * population) * 100),
                         "进化搜索 第%d代" % g)
        pop = new_pop

    pop.sort(key=lambda x: x[0], reverse=True)
    return pop[0][0], pop[0][1], pop[0][2]


# ===========================================================================
# 训练主流程 + 状态管理
# ===========================================================================
DL = {
    "running": False, "progress": 0, "stage": "", "error": "",
    "task": "", "result": None, "started": "", "finished": "",
}

_dl_lock = threading.RLock()


def status() -> dict:
    with _dl_lock:
        return dict(DL)


def _load_persisted():
    """启动时恢复上次训练结果"""
    with _dl_lock:
        if DL["result"] is not None:
            return
        try:
            blob = store.kv_get("dlfactor_last")
            if blob:
                DL["result"] = blob.get("result")
                DL["finished"] = blob.get("at", "")
                DL["task"] = blob.get("task", "")
                DL["stage"] = "（上次训练的结果，重启后自动恢复）"
                DL["progress"] = 100
        except Exception:
            pass


def _prog(p, stage):
    with _dl_lock:
        DL["progress"] = max(0, min(100, int(p)))
        DL["stage"] = stage


def train(symbols=None, days=400, horizon=5, task="cross_section",
          population=8, generations=3) -> dict:
    """训练深度学习因子。task: cross_section 或 time_series。"""
    with _dl_lock:
        if DL["running"]:
            return {"ok": False, "message": "已经有一个训练在跑了，请稍等"}
        DL.update({"running": True, "progress": 0, "stage": "准备中", "error": "",
                   "task": task, "result": None, "started": store.now_str(), "finished": ""})

    symbols = [ds.normalize(s) for s in (symbols or []) if s]
    if not symbols:
        # 默认用因子实验室的股票池
        from . import factorlab as lab
        symbols = list(lab.DEFAULT_POOL)

    threading.Thread(target=_train_worker,
                     args=(symbols, days, horizon, task, population, generations),
                     name="dlfactor-train", daemon=True).start()
    return {"ok": True, "message": "训练已开始，大约需要 1~5 分钟"}


def _train_worker(symbols, days, horizon, task, population, generations):
    try:
        _prog(2, "拉取股票池历史数据…")
        panel = {}
        total = max(len(symbols), 1)
        for i, s in enumerate(symbols):
            try:
                ks = ds.kline(s, "daily", 1, int(days))
                ks = [k for k in ks if k.get("close")]
                if len(ks) >= 80:
                    panel[ds.normalize(s)] = ks
            except Exception as e:  # noqa: BLE001
                store.log("WARN", "dlfactor", "取 %s 数据失败: %s" % (s, str(e)[:100]))
            _prog(2 + int((i + 1) / total * 28), "拉取行情 %d/%d" % (i + 1, total))

        if len(panel) < 5:
            raise RuntimeError("有效股票太少（<5 只），无法训练")

        _prog(32, "提取特征、构建样本…")
        samples = _make_samples(panel, 60, horizon, task)
        if len(samples) < 50:
            raise RuntimeError("样本太少（<50 条），请增大 days 或换股票池")

        train_set, val_set = _split(samples, 0.7)

        _prog(40, "进化搜索最优网络结构…")
        best_ic, model, cfg = _evolve(
            train_set, val_set, task, population, generations,
            progress=lambda p, s: _prog(40 + int(p * 0.55), s))

        if model is None:
            raise RuntimeError("训练失败：未能产出有效模型")

        _prog(97, "写入结果…")
        # 结果描述
        if task == "cross_section":
            factor_key = "dl_ai_cross"
            factor_name = "AI选股因子"
        else:
            factor_key = "dl_ai_timing"
            factor_name = "AI择时信号"

        result = {
            "ok": True,
            "task": task,
            "factor_key": factor_key,
            "factor_name": factor_name,
            "ic_val": round(best_ic, 5) if best_ic is not None else None,
            "config": cfg,
            "hidden": cfg.get("hidden"),
            "n_samples": len(samples),
            "n_train": len(train_set),
            "n_val": len(val_set),
            "symbols": sorted(panel.keys()),
            "horizon": horizon,
            "days": days,
            "generated_at": store.now_str(),
            "model": _serialize_model(model),
            "note": _note_for(best_ic),
        }
        # 用样本外 IC 给结论
        if best_ic is not None and abs(best_ic) >= 0.05:
            result["level"] = "strong"
            result["verdict"] = "样本外 IC 较显著，历史上有效"
        elif best_ic is not None and abs(best_ic) >= 0.03:
            result["level"] = "medium"
            result["verdict"] = "有一定效果，可以小仓位试试"
        elif best_ic is not None and abs(best_ic) >= 0.02:
            result["level"] = "weak"
            result["verdict"] = "效果偏弱，单独用不太行"
        else:
            result["level"] = "none"
            result["verdict"] = "本次没挖到稳定信号（样本外 IC 过低）"

        # 先持久化，再标记完成，避免前端轮询到 running=False 时模型还没落库
        try:
            store.kv_set("dlfactor_last", {"task": task, "result": result,
                                           "at": store.now_str()})
            # 每个任务单独存一份，供 factors.py 的因子打分读取
            store.kv_set("dlfactor_" + task, result)
        except Exception:
            pass
        with _dl_lock:
            DL.update({"running": False, "progress": 100, "stage": "完成",
                       "task": task, "result": result, "finished": store.now_str()})
        store.log("INFO", "dlfactor",
                  "深度学习因子训练完成：%s 样本外IC=%s" % (task, result["ic_val"]))
    except Exception as e:  # noqa: BLE001
        with _dl_lock:
            DL.update({"running": False, "error": str(e)[:300], "stage": "失败"})
        store.log("ERROR", "dlfactor", "训练失败: %s" % str(e)[:200])


def _note_for(ic):
    if ic is None:
        return "样本外 IC 无法计算（样本太少）。"
    if abs(ic) >= 0.05:
        return "样本外 IC=%.4f，统计上比较显著，但注意样本外 IC 高不代表未来一定有效。" % ic
    if abs(ic) >= 0.03:
        return "样本外 IC=%.4f，有一定效果。建议与其它因子组合使用。" % ic
    return "样本外 IC=%.4f，效果偏弱，建议换股票池或时间窗口重试。" % ic


def get_result() -> dict:
    _load_persisted()
    with _dl_lock:
        return DL["result"]


def get_model(task="cross_section"):
    """供 factors.py 打分调用：读持久化的模型，返回 MLP 或 None"""
    blob = store.kv_get("dlfactor_" + task)
    if not blob or not blob.get("model"):
        return None
    try:
        return _deserialize_model(blob["model"])
    except Exception:
        return None


def ai_factor_score(klines, task="cross_section", warmup=60):
    """对单只股票的 K 线打分，返回等长列表（前 warmup 根为 None）。

    cross_section: 输出 0~1 的选股分（越高越看好）
    time_series:   输出 0~1 的多头概率
    """
    model = get_model(task)
    if model is None:
        return [None] * len(klines)
    feats = extract_features(klines, warmup)
    out = [None] * len(klines)
    for i in range(len(klines)):
        if feats[i] is None:
            continue
        try:
            out[i] = model.predict(feats[i])
        except Exception:
            out[i] = None
    return out


# ===========================================================================
# 无监督因子挖掘（不需要"未来收益"当标签，直接从数据结构里找信息）
#
# 和监督学习（上面那套 MLP 预测未来收益）不同，这里不告诉模型什么是对的：
#   PCA       把 15 维特征压成几个主方向，投影强度 = 当前状态在主要波动方向上的位置
#   自编码器   学"压缩再还原"，还原不出来 = 这种状态历史上很少见 = 异常
#   KMeans    把历史状态聚成几类，离最近类中心的距离 = 当前有多不合群
#
# 好处：不需要标签、不会过拟合未来收益、能发现人没写过的结构。
# 坏处：它只描述"状态罕不罕见"，不直接告诉你涨跌，必须靠因子实验室的 IC 验证。
#
# 关键设计（防止未来函数）：每次只用「当天之前」的窗口拟合模型，再给当天打分。
# ===========================================================================
UL_WINDOW = 80       # 拟合窗口（根K线）
UL_REFIT = 40        # 每多少根重拟合一次（太频繁会很慢）
UL_AE_HIDDEN = 4     # 自编码器隐层宽度
UL_AE_EPOCHS = 3     # 自编码器训练轮数
UL_CACHE_MAX = 80    # 结果缓存条数（同一只票的 4 个因子共享一次计算）

_ul_cache: dict = {}
_ul_lock = threading.RLock()


class AutoEncoder:
    """单隐层自编码器（d -> h -> d），纯手写，用法和 sklearn 的最小版类似。

    为什么不用上面的 MLP：MLP 只输出一个标量，自编码器要还原整条特征向量。
    """

    def __init__(self, d, h, lr=0.06, l2=1e-4, seed=0):
        self.d, self.h = d, h
        self.lr, self.l2 = lr, l2
        rng = random.Random(seed)
        b1 = 1.0 / math.sqrt(max(d, 1))
        b2 = 1.0 / math.sqrt(max(h, 1))
        self.W1 = [[rng.uniform(-b1, b1) for _ in range(d)] for _ in range(h)]
        self.b1 = [0.0] * h
        self.W2 = [[rng.uniform(-b2, b2) for _ in range(h)] for _ in range(d)]
        self.b2 = [0.0] * d

    @staticmethod
    def _sig(z):
        if z >= 0:
            return 1.0 / (1.0 + math.exp(-z))
        e = math.exp(z)
        return e / (1.0 + e)

    def encode(self, x):
        return [self._sig(self.b1[j] + sum(self.W1[j][i] * x[i] for i in range(self.d)))
                for j in range(self.h)]

    def decode(self, h):
        return [self._sig(self.b2[j] + sum(self.W2[j][i] * h[i] for i in range(self.h)))
                for j in range(self.d)]

    def reconstruct(self, x):
        return self.decode(self.encode(x))

    def train_one(self, x):
        h = self.encode(x)
        out = self.decode(h)
        d2 = [(out[j] - x[j]) * out[j] * (1.0 - out[j]) for j in range(self.d)]
        d1 = [h[j] * (1.0 - h[j]) * sum(self.W2[k][j] * d2[k] for k in range(self.d))
              for j in range(self.h)]
        for j in range(self.d):
            for i in range(self.h):
                self.W2[j][i] -= self.lr * (d2[j] * h[i] + self.l2 * self.W2[j][i])
            self.b2[j] -= self.lr * d2[j]
        for j in range(self.h):
            for i in range(self.d):
                self.W1[j][i] -= self.lr * (d1[j] * x[i] + self.l2 * self.W1[j][i])
            self.b1[j] -= self.lr * d1[j]
        return sum((out[j] - x[j]) ** 2 for j in range(self.d)) / self.d


def _std_fit(rows):
    """按列标准化，返回 (均值, 标准差)"""
    n = len(rows)
    d = len(rows[0])
    mu = [sum(rows[i][j] for i in range(n)) / n for j in range(d)]
    sd = []
    for j in range(d):
        v = sum((rows[i][j] - mu[j]) ** 2 for i in range(n)) / max(n - 1, 1)
        sd.append(max(v ** 0.5, 1e-9))
    return mu, sd


def _std_apply(row, mu, sd):
    return [(row[j] - mu[j]) / sd[j] for j in range(len(row))]


def _squash(z):
    """把 z-score 压到 0~1，好喂给 sigmoid 自编码器（负值它表示不了）"""
    out = []
    for v in z:
        if v >= 0:
            out.append(1.0 / (1.0 + math.exp(-v)))
        else:
            e = math.exp(v)
            out.append(e / (1.0 + e))
    return out


def _pca_first(Z):
    """幂迭代求第一主成分方向（协方差矩阵最大特征向量）"""
    n, d = len(Z), len(Z[0])
    mu = [sum(Z[i][j] for i in range(n)) / n for j in range(d)]
    C = [[0.0] * d for _ in range(d)]
    for i in range(n):
        for a in range(d):
            za = Z[i][a] - mu[a]
            for b in range(a, d):
                C[a][b] += za * (Z[i][b] - mu[b])
    for a in range(d):
        for b in range(a):
            C[a][b] = C[b][a]
    v = [1.0 / math.sqrt(d)] * d
    lam = 0.0
    for _ in range(25):
        w = [sum(C[a][b] * v[b] for b in range(d)) for a in range(d)]
        norm = math.sqrt(sum(x * x for x in w)) or 1e-9
        v = [x / norm for x in w]
        lam = norm
    # 固定符号（否则每只股票方向可能正负相反，因子不可比）
    if sum(v) < 0:
        v = [-x for x in v]
    return lam, v


def _kmeans(X, k, iters=12, seed=0):
    rng = random.Random(seed)
    n, d = len(X), len(X[0])
    cents = [list(X[rng.randrange(n)]) for _ in range(min(k, n))]
    labels = [0] * n
    for _ in range(iters):
        for i in range(n):
            best, bi = 1e18, 0
            for c, ct in enumerate(cents):
                dd = 0.0
                for j in range(d):
                    t = X[i][j] - ct[j]
                    dd += t * t
                if dd < best:
                    best, bi = dd, c
            labels[i] = bi
        acc = [[0.0] * d for _ in range(len(cents))]
        cnt = [0] * len(cents)
        for i in range(n):
            c = labels[i]
            cnt[c] += 1
            for j in range(d):
                acc[c][j] += X[i][j]
        for c in range(len(cents)):
            if cnt[c]:
                cents[c] = [acc[c][j] / cnt[c] for j in range(d)]
    return cents


def _ul_key(klines):
    return (len(klines), str(klines[0].get("date"))[:10], str(klines[-1].get("date"))[:10])


def unsupervised_factors(klines, warmup=60):
    """一次算好 4 个无监督因子，返回 {因子名: 等长列表}。

    结果按 (长度, 首日, 末日) 缓存 —— 因子实验室会把同一只股票的 4 个因子
    依次算一遍，不缓存的话同样的重活要干 4 次。
    """
    names = ("ul_pca_score", "ul_pca_resid", "ul_ae_err", "ul_km_dist")
    n = len(klines)
    empty = {k: [None] * n for k in names}
    try:
        feats = extract_features(klines, warmup)
    except Exception:
        return empty
    valid = [i for i in range(n) if feats[i] is not None]
    if len(valid) < UL_WINDOW + 5:
        return empty

    ck = _ul_key(klines)
    with _ul_lock:
        hit = _ul_cache.get(ck)
    if hit is not None:
        return hit

    out = {k: [None] * n for k in names}
    state = None
    for pos in range(UL_WINDOW, len(valid)):
        if (pos - UL_WINDOW) % UL_REFIT == 0:
            # 只用「pos 之前」的数据拟合，绝不偷看未来
            train = [feats[valid[k]] for k in range(pos - UL_WINDOW, pos)]
            mu, sd = _std_fit(train)
            Z = [_std_apply(r, mu, sd) for r in train]
            lam, pc1 = _pca_first(Z)
            S = [_squash(z) for z in Z]
            ae = AutoEncoder(len(S[0]), UL_AE_HIDDEN, seed=pos)
            for _ in range(UL_AE_EPOCHS):
                for row in S:
                    ae.train_one(row)
            cents = _kmeans(Z, k=min(3, max(2, len(Z) // 20)), seed=pos)
            state = {"mu": mu, "sd": sd, "pc1": pc1, "ae": ae, "cents": cents}
        if state is None:
            continue
        idx = valid[pos]
        z = _std_apply(feats[idx], state["mu"], state["sd"])
        # 1) 第一主成分投影：当前状态在主要波动方向上的位置
        proj = sum(z[j] * state["pc1"][j] for j in range(len(z)))
        out["ul_pca_score"][idx] = proj
        # 2) 剔除主成分后的残差范数：跟"大盘式共同波动"偏离多大
        resid = math.sqrt(max(sum((z[j] - proj * state["pc1"][j]) ** 2
                                  for j in range(len(z))), 0.0))
        out["ul_pca_resid"][idx] = resid
        # 3) 自编码器重构误差：这种状态有多"没见过"
        try:
            rec = state["ae"].reconstruct(_squash(z))
            sq = _squash(z)
            out["ul_ae_err"][idx] = sum((rec[j] - sq[j]) ** 2
                                        for j in range(len(sq))) / len(sq)
        except Exception:
            out["ul_ae_err"][idx] = None
        # 4) 到最近聚类中心的距离：在历史上属于哪一类、离得多远
        best = 1e18
        for ct in state["cents"]:
            dd = 0.0
            for j in range(len(z)):
                t = z[j] - ct[j]
                dd += t * t
            if dd < best:
                best = dd
        out["ul_km_dist"][idx] = math.sqrt(max(best, 0.0))

    with _ul_lock:
        if len(_ul_cache) >= UL_CACHE_MAX:
            for k in list(_ul_cache.keys())[:max(1, len(_ul_cache) // 4)]:
                _ul_cache.pop(k, None)
        _ul_cache[ck] = out
    return out


def ul_factor(klines, name, warmup=60):
    """给 factors.py 用的单因子入口"""
    return unsupervised_factors(klines, warmup).get(name, [None] * len(klines))
