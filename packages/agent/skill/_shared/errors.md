# 错误处理

- `NO_CONFIG`：必须改用 bootstrap 生成的启动器。
- `NO_TASK`：正常结束本轮。
- `AT_CAPACITY`：本轮结束，由原在途会话继续；不执行 `status`、`inspect`、`claim` 或再次 `dispatch`。
- `REJECTED`：读取下一轮 bundle 的 `previous_failures`，修复后重交。
- `CONFLICT`：租约已失效，停止写入并重新领取。
