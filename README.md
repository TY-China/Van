
## 🔧 **适配说明**

### **主要改动：**

1. **事件处理**：
   - 使用 `@filter.event_message_type` 替代 NoneBot 的 `on_message`
   - 使用 `AstrMessageEvent` 替代 `GroupMessageEvent/PrivateMessageEvent`

2. **消息组件**：
   - 使用 `MessageChain` 和 `Plain`, `Image`, `At` 等组件
   - 支持 AstrBot 的统一消息模型

3. **配置系统**：
   - 使用 AstrBot 的配置系统 (`_conf_schema.json`)
   - 配置可在 WebUI 中可视化修改

4. **指令系统**：
   - 支持 AstrBot 的指令组系统
   - 保持原有的管理员指令兼容性

5. **权限管理**：
   - 使用 `@filter.permission_type(filter.PermissionType.ADMIN)`
   - 同时支持配置中的管理员列表

### **平台兼容性：**
- ✅ QQ 个人号（OneBot v11）
- ✅ QQ 官方机器人

### **保留的功能：**
- 所有关键词匹配模式
- 变量替换系统
- 特殊语法处理
- 媒体消息支持
- 冷却时间控制
- 词库管理功能

这个移植版本保留了原插件的核心功能，同时完全适配 AstrBot 的架构，可以在 AstrBot 中稳定运行。