# 用外部定时器准点触发（解决 GitHub cron 漂移）

GitHub 自带的 `schedule` 会延迟 5~15 分钟，甚至偶尔跳过。
外部服务准点 `POST` 一次 `workflow_dispatch`，从触发到开始跑通常 **5~20 秒**。

---

## 第 0 步：建一个"只能触发这个仓库 Actions"的令牌

**不要用账号密码，也不要建全权限的经典 Token。**

1. 打开 https://github.com/settings/personal-access-tokens/new （Fine-grained PAT）
2. **Token name**：`trigger-stock-scan`
3. **Expiration**：90 天（到期重建；别选 No expiration）
4. **Repository access** → `Only select repositories` → 只选你的这个仓库
5. **Permissions** → 展开 `Repository permissions` → 找到 **Actions** → 改成 **Read and write**
   （其他全部保持 No access）
6. 点 `Generate token`，**立刻复制**（`github_pat_...`，只显示一次）

> 这个令牌只能干一件事：触发你这个仓库的工作流。泄露了也不能读代码、不能改代码。

---

## 第 1 步：先用 curl 验证一次

把三处 `<>` 换成你的：

```bash
curl -i -X POST \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer <你的PAT>" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  https://api.github.com/repos/<用户名>/<仓库名>/actions/workflows/stock.yml/dispatches \
  -d '{"ref":"main","inputs":{"mode":"scan"}}'
```

**期望结果**：`HTTP/2 204`（无响应体就是成功）。

然后在浏览器打开仓库的 **Actions** 页面，应该立刻看到一条新的运行，
触发者显示 `you`，备注里是 `workflow_dispatch`。

常见错误：

| 返回 | 原因 |
| --- | --- |
| `401` | PAT 错/过期/没复制全 |
| `403` | PAT 没给 Actions: write，或没勾这个仓库 |
| `404` | 仓库名/工作流文件名错（文件名是 `stock.yml`，不是标题） |
| `422` | 分支名不对（不是 `main`），或 `inputs` 里的 `mode` 值不在 [auto,scan,rank] 里 |

---

## 第 2 步：选一个外部定时器

### 方案 A：cron-job.org（零代码，5 分钟搞定）

1. 注册 https://cron-job.org（免费）
2. `Create cronjob`
3. **Title**：`stock-scan-15m`
4. **URL**：`https://api.github.com/repos/<用户名>/<仓库名>/actions/workflows/stock.yml/dispatches`
5. **Schedule**：`Every 15 minutes`；时区选 `Asia/Shanghai`
6. **Request method**：改成 **POST**
7. **Request body**：`{"ref":"main","inputs":{"mode":"scan"}}`
8. **Headers** 加三条：
   ```
   Accept: application/vnd.github+json
   Authorization: Bearer <你的PAT>
   Content-Type: application/json
   ```
9. **限制执行时段**：Advanced → `Execute only between` 设为 `09:45` ~ `15:00`，周一到周五
10. 保存，点 `TEST RUN` 看返回码是不是 204

> 免费版够用（这个任务每交易日只跑 16 次）。注意它默认可能重试，重试也无害——
> 工作流有 `concurrency` 锁，第二个会排队而不是并行。

### 方案 B：Cloudflare Worker + Cron（免费，最稳，可写代码）

1. Cloudflare 控制台 → `Workers & Pages` → `Create` → `Worker`
2. 粘贴下面代码，把常量改成你的：

```js
const OWNER  = "你的用户名";
const REPO   = "你的仓库名";
const REF    = "main";
const WF     = "stock.yml";
const TOKEN  = "github_pat_xxx";      // 建议用 Worker 的 Secret 而不是明文

export default {
  async scheduled(event, env, ctx) {
    const r = await fetch(
      `https://api.github.com/repos/${OWNER}/${REPO}/actions/workflows/${WF}/dispatches`,
      {
        method: "POST",
        headers: {
          "Accept": "application/vnd.github+json",
          "Authorization": `Bearer ${TOKEN}`,
          "X-GitHub-Api-Version": "2022-11-28",
          "User-Agent": "cf-worker",
        },
        body: JSON.stringify({ ref: REF, inputs: { mode: "scan" } }),
      }
    );
    console.log("dispatch", r.status);
  },
};
```

3. `Settings → Variables and Secrets` → 把 TOKEN 加成 **Secret**
4. `Settings → Triggers → Cron Triggers` 加三条（UTC！）：
   - `45,0,15,30 1-3 * * 1-5`（北京 09:45~11:30）
   - `15,30,45 5,6 * * 1-5`（北京 13:15~14:45）
   - `0 7 * * 1-5`（北京 15:00 收官）
5. 晚上那次再加一条：`30 14 * * 1-5`（北京 22:30），body 里 `mode` 改成 `rank`

### 方案 C：Google Apps Script（有 Google 账号就能用，零部署）

1. 打开 https://script.google.com → `New project`
2. 粘贴：

```javascript
const OWNER = '你的用户名';
const REPO  = '你的仓库名';
const TOKEN = 'github_pat_xxx';

function triggerScan() {
  UrlFetchApp.fetch(
    `https://api.github.com/repos/${OWNER}/${REPO}/actions/workflows/stock.yml/dispatches`,
    {
      method: 'post',
      contentType: 'application/json',
      headers: {
        Authorization: `Bearer ${TOKEN}`,
        Accept: 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
      },
      payload: JSON.stringify({ ref: 'main', inputs: { mode: 'scan' } }),
      muteHttpExceptions: true,
    }
  );
}
```

3. 左侧 `Triggers`（闹钟图标）→ `Add Trigger`
   - Function: `triggerScan`
   - Event source: `Time-driven`
   - Type: `Minutes timer` → `Every 15 minutes`
4. 保存时会要授权，允许即可

> Apps Script 的 15 分钟是"大约"，但通常比 GitHub cron 准得多。

---

## 第 3 步：验证

| 检查 | 方法 |
| --- | --- |
| 触发成功 | Actions 页面出现新运行，触发者是你 |
| 时间准点 | 对比运行开始时间和 09:45/10:00 这些整点 |
| 数据更新 | 手机打开 Pages 网址，页面顶部"生成于"的时间变了 |
| 没有并行 | 同时触发两次，第二次应显示 `queued` 而不是 `in_progress` |

---

## 第 4 步：注意事项

1. **两路并存没问题**：GitHub 自带 cron 保留作兜底，`concurrency` 会保证不并行。
2. **PAT 到期**：90 天后触发会 401。在日历上记一笔，重建后更新到定时服务里。
3. **配额**：公开仓库 Actions 免费不限；私有仓库 2000 分钟/月，每交易日 16 次 × 3 分钟 ≈ 1000 分钟，够用。
4. **晚上那次别用 `scan`**：`mode=rank` 才会跑收盘选股（要 5~10 分钟 + 大量 Tushare 调用）。
   白天用 `scan`，晚上用 `rank`，别搞反。
5. **PAT 放哪**：cron-job.org 是明文存在它的服务器上；Cloudflare Worker 用 Secret；
   Apps Script 写在脚本里（只有你能看）。安全性：Worker > Apps Script > cron-job.org。
