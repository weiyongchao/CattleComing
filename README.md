# 量价罗盘：A 股实时评测

一个零第三方依赖的本地 Web 项目。输入 6 位 A 股代码，读取实时成交价、涨跌幅、成交量及近 30 个交易日行情，以五日均线和五日均量生成可解释的 0–100 分规则评测。

## 启动

要求 Python 3.10 或更高版本：

```powershell
python run.py
```

浏览器打开 <http://127.0.0.1:8000>。也可以指定端口：

```powershell
python run.py --port 8080
```

运行测试：

```powershell
python -m unittest discover -s tests -v
```

## 评分模型

基础分为 50 分，六项信号叠加后限制在 0–100：

- MA5 趋势（±20）：现价相对五日均线的位置。
- 今日动量（±15）：实时涨跌幅。
- 昨日动量（±10）：最近完整交易日的涨跌幅。
- 量比（±10）：当前成交量相对近五日平均成交量；上涨放量加分，下跌放量减分。
- 换手率（±5）：交投活跃度对评分的有限修正。
- 日内位置（±10）：现价处于今日最低价至最高价区间的位置。

评分是透明的启发式规则，不是机器学习预测，也不构成投资建议。盘中累计成交量直接与完整交易日均量比较，早盘量比通常偏低，这是模型边界之一。

页面还会根据评分、MA5、量比、换手率和日内位置，分别生成未持仓建仓参考、已有仓位加仓/持有参考、确认条件、失效条件及动态风险点。操作提示不包含个人风险承受能力、投资期限和整体资产配置，不能替代个人投资决策。

## 数据与参考项目

- 行情数据：东方财富公开行情页面的实时行情接口；连接失败时自动降级到腾讯实时行情。历史数据使用腾讯证券公开页面的前复权日线接口。接口可能调整，仅限学习研究。
- [AKShare](https://github.com/akfamily/akshare)：A 股实时/历史数据接口设计参考。
- [yfinance](https://github.com/ranaroussi/yfinance)：全球证券行情数据封装参考。
- [ai-stock-dashboard](https://github.com/ErikThiart/ai-stock-dashboard)：技术指标仪表盘与评分展示参考。

## 目录

```text
run.py                         启动入口
src/stock_evaluator/market.py  行情访问和代码解析
src/stock_evaluator/evaluator.py 指标与评分逻辑
src/stock_evaluator/server.py  HTTP API 与静态文件服务
web/                           仪表盘前端
tests/                         离线单元测试
```
