# 声明式订阅（订阅化阶段一）

新增一个 RSS 源 = 在本目录放一个 YAML 文件。**热加载**：新增/修改/删除文件
在下一轮采集自动生效（按文件 mtime 检测），无需改 `config.yaml`、无需重启。

## 完整示例（全字段 + 默认值）

```yaml
apiVersion: ttsniffer/v1     # 必填，当前唯一版本
kind: Subscription           # 可省略（默认即它）
metadata:
  id: my-feed                # 必填。^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$，不可变——
                             # 水位与去重的锚，改 id 等于新订阅；同 id 重复先到先得
  name: 我的订阅              # 可选。显示名（≤32 字），缺省 = id
spec:
  kind: rss                  # 可选。阶段一只支持 rss
  url: https://example.com/feed   # 必填。http/https
  source: my_feed            # 可选。入库来源标签（前端筛选/可用性面板用它，≤32 字），
                             # 缺省 = id；与 config.yaml 里 feeds 的 source 同语义
  tier: street               # 可选。street=⚠️未证实标+不进打扰通道；authoritative=正经源。
                             # 缺省 street——没把握的源默认当小道处理
  max_age_days: 2            # 可选。首轮回灌只收近 N 天，防旧文刷屏（默认 2）
  interval: 600              # 可选。期望轮询秒数（默认 600；整段取各订阅最小值，
                             # 下限 60——别用一份高频订阅把整段拖快）
  enabled: true              # 可选。false=跳过该订阅（文件保留，随时改回）
```

## 边界纪律

- 校验失败的文件**跳过并记 warning**，不影响其余订阅与采集主链路；
- 阶段一不做代码插件、不做 per-sub 独立调度（见
  `docs/reports/20261023_订阅插件化架构调研.md` 的分层结论）；
- 抓取礼貌沿用 RSS 采集器既有语义：逐 feed 容错、单源失败不毒整轮。
