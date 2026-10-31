# I2 创作档案与系列连续性

每个受委托空间可配置最多 8 个创作档案和 8 个系列。配置不会启用自主创作或增加额度；没有档案时沿用原有 persona 与计划行为。插件配置版本为 0.9.0，旧配置补入空列表。

## 配置与创作选择

在插件面板的 `autonomy.spaces` 对象中增加 `profiles` 和 `series`。下例只展示新增字段；真实身份、持续委托和预算仍按 [阶段 6](implementation-stage-6.md) 配置。

```json
{
  "profiles": [{
    "profile_id": "rain",
    "revision": 1,
    "data": {
      "title": "雨夜练习",
      "palette": "暖黄与低饱和蓝",
      "technique": "柔和水彩",
      "stable": {"subject": "窗边小猫"},
      "variable": {"light": "柔光"},
      "source": "管理员配置"
    }
  }],
  "series": [{
    "series_id": "windows",
    "title": "窗边天气",
    "goal": "观察不同天气下的光线",
    "profile_id": "rain",
    "profile_revision": 1
  }]
}
```

同一 profile_id/revision 的内容不可覆盖；修改档案须增加 revision。稳定属性和可变属性不能使用同名键。移除配置只停用后续选择，历史档案和已冻结的作品仍保留。系列绑定具体档案版本，其目标不可原地改写；结束的系列不会因重载相同配置重新打开。

模型在 create 建议中可选 `creative={profile_id,profile_revision,series_id,changes,reason}`。series_id 可以为 null；changes 只能指定该档案允许变化的键，不能覆盖 stable 属性。模型也可选择等待、创作其他主题或以 close_series 结束系列；活动作品未完成时不能借关闭系列跳过其状态。

## 实际执行

`profiles.py` 负责纯数据校验和确定性提示词组织。`ArtistStore` 在最新授权下验证配置及模型选择，把档案原文、系列目标和本次变化冻结到 `brief.creative`。同系列中归一化后相同的意图与主体不能重复创建作品；这是确定性去重，不保证识别所有语义相似的改写。

新图和原图编辑均由冻结档案补充配色、表现方式、稳定属性及本次变化。最终提示词超过 MCP 的 4000 字符限制时明确停止，不截断、换图或偷偷重画。无档案时原提示词逐字保持不变。

评审看到同一冻结 brief，因此配置更新不会改变旧作品的完成标准。语义上的人格冲突、艺术风格是否真正一致仍需模型判断；程序保证字段优先级、不可修改的稳定属性和权限边界，不宣称能形式化证明审美一致性。

## 存储与验证

schema 4 追加档案版本、系列及作品关联，旧快照、操作、预算和发送记录不变。配置同步为事务操作；跨空间不可引用，档案或系列变更不产生模型调用。生命周期仍由 HostRuntime 管理，不新增计时器或独立数据库。

使用合成模型和绘图测试配置同步、不可变版本、关闭系列、稳定属性拒绝修改、计划冻结后更新配置、实际绘图提示词及评审一致性。测试入口为 `tests/test_profiles_store.py` 和 `tests/test_profiles_flow.py`，并回归已有 I1 和 Host 流程。实际验证结论见 [validation.md](validation.md)；没有部署或真实视觉效果验收。
