# 临期轮转开车接口与链路总览

> 性质：`chatgpt.com` / `auth.openai.com` 的 Web **内部接口**，字段与服务端校验随时可能变；

> 用量探测与 sub2api 交付接口为自有实现。

链路顺序：**邀请（拉人进空间）→ OAuth（拿凭据 AT / RT）→ 轮转（踢旧 → 补新 → 交付）**。

三个系统总览：

| 系统             | Base                    | 用途                              |
| --- | --- | --- |
| ChatGPT 业务接口 | https://chatgpt.com     | 成员/席位/邀请/踢人/上车          |
| OpenAI 认证接口  | https://auth.openai.com | 登录、MFA、workspace select、PKCE |
| sub2api 交付端点 | \<端点 BaseURL\>/api/v1 | 轮转账号上传/更新（交付）         |

## 0. 通用约定

- **两类 AT**：personal AT（个人 access_token）与 workspace AT（空间 access_token）。读取类用 owner/reader 的 workspace AT；自退用**子号自己的** workspace AT。

- **团队接口必须带** chatgpt-account-id: \<workspace_id\>，且必须与 URL 里的 account_id 一致。

- **浏览器语义头**（缺失易被 Cloudflare 以 403 HTML 拦掉）：

User-Agent / Origin: https://chatgpt.com / Referer / Sec-Fetch-Site\|Mode\|Dest / oai-device-id / oai-language / Priority: u=1, i。

- **失败语义**（轮转代码里的分类口径）：

| 现象 | 含义 | 处置 |
| --- | --- | --- |
| 401 token_revoked | AT 失效 | 刷新 AT，或用「账号密码 + 2FA（TOTP）」重登 |
| 403（HTML） | Cloudflare 挑战 / 出口被封禁地区 | 换出口；重登链路用 GateOnly 降级 |
| 409 | 并发成员变更瞬时冲突，**不是权限拒绝** | 退避重试（attempt×5s → 5/10/15s，最多 3 次） |
| 429 | 限流（邀请接口 per-workspace 明显） | 退避 + 降频 |
| deactivated_workspace / workspace_not_found | 空间终态 | 停止对该空间操作 |
| seat_true_up_pending | 付费席位已满 | 先扩容/踢人再邀请 |

## 1. 邀请（拉人进空间）

### 1.1 批量邀请（owner workspace token 群发邀请邮件）

用 owner workspace token 向空间群发邀请邮件，是铺量/补池的主入口。

**接口**

POST /backend-api/accounts/{workspace_id}/invites

- 认证：Authorization: Bearer \<owner workspace AT\>（自动取母号交换出的 workspace token，可手动粘贴兜底）

- body（一次 1~100 个邮箱，默认 25/请求）：

```json
{
"email_addresses": ["a@example.com", "b@example.com"],
"flow_id": "{uuid}",
"role": "standard-user",
"seat_type": "default",
"resend_emails": true,
"submission_id": "{uuid}"
}
```

- 头：Origin: https://chatgpt.com + Referer: https://chatgpt.com/admin/members?tab=members + oai-client-build-number / oai-client-version / oai-device-id / oai-session-id / oai-language / x-oai-is-client-observation / x-oai-is-pending-updates / x-openai-target-path / x-openai-target-route

- HTTP 200 不代表全成功：要看响应里的 account_invites（真成功数）与 errored_emails（静默失败）

**队列与重试语义**

- 出队终态：HTTP 200 / 401 / 422 / 其他 4xx

- 回队重试：429 / 网络错误 / 5xx —— 在重试窗口内**不限回队次数**

- 组级硬截断：自该组首次可重试失败起计窗口（默认 5 分钟），到期整组丢弃出队

- 请求级退避封顶：429 指数退避（基数 3s、封顶 30s）；网络/5xx 线性退避（基数 1s、封顶 10s）；退避等待不越过组级窗口截止

- 自适应并发：最近 100 次请求的 429 占比 \>60% 全局退避 2s、\>40% 退避 1s

- **全程固定单个代理 IP** 与单个 session；连续网络失败 3 次只重建 HTTP 传输会话（IP 不变）

- 默认并发 50（上限 200），分组大小 25（上限 100）

**启动前预检**

- 席位预检（GET .../users/seat_type_counts）：只留档、不硬阻断；真实超卖会在运行中以 401 true-up 暴露

### 1.2 邀请维护（清死号 + 补邀请）

| 动作 | 接口 | 关键 body |
| --- | --- | --- |
| 批量补邀请 | POST /backend-api/accounts/{ws}/invites | {"email_addresses":\[...\],"flow_id":uuid,"role":"standard-user","seat_type":"default","resend_emails":true,"submission_id":uuid} |
| 删除邀请 / 拒绝请求 | DELETE /backend-api/accounts/{ws}/invites | {"email_address":"\<email\>"}（上游只认邮箱，不认 invite id） |
| 接受加入请求 | PATCH /backend-api/accounts/{ws}/invites/{invite_id} | {"accept_request":true,"role":"standard-user","seat_type":"default"} |

- 200 不代表全成功：POST /invites 要看响应里的 account_invites（成功数）与 errored_emails。

- 轮转维护节奏：最小间隔 10 分钟 / 单轮预算 5 分钟 / 批量执行；邀请接口有 per-workspace 限流（429 实证），过频会触发 CF 403。

## 2. OAuth（拿凭据）

### 2.1 账号密码 + 2FA（TOTP）登录

POST https://auth.openai.com/api/accounts/password/verify

POST https://auth.openai.com/api/accounts/mfa/verify （首选）

POST https://auth.openai.com/api/accounts/mfa/challenge/{id}/verify （变体）

POST https://auth.openai.com/api/accounts/mfa/totp/verify （变体）

GET https://auth.openai.com/api/accounts/client_auth_session_dump

- password/verify 先发一次"sentinel"（预期 401）再发真密码，对齐浏览器行为。

### 2.2 OAuth 授权路径（authorize → callback → token）

**① authorize（Codex 式）**

GET https://auth.openai.com/oauth/authorize

?response_type=code

&client_id=app_EMoamEEZ73f0CkXaXp7hrann

&redirect_uri=http://localhost:1455/auth/callback

&scope=openid profile email offline_access api.connectors.read api.connectors.invoke

&code_challenge={challenge}

&code_challenge_method=S256

&state={随机}

&id_token_add_organizations=true

&codex_cli_simplified_flow=true

&originator=codex_cli_rs

可选参数：prompt=login（显式确认登录态；撞绑手机门禁时去掉重试）、login_hint={email}（跳过 choose-an-account）、allowed_workspace_id={workspace_id}（锁定目标工作区的授权变体）。

**② 跟随重定向截获 code**

- 在已登录 session 上手动逐跳跟随（上限 12 跳），从 redirect_uri?code=...&state=... 取出 code；

- 终态停在 /email-verification（"会话已结束"）→ 登录态 cookie 已过期，需重登；

- 命中 /add-phone（绑手机门禁）→ 该账号跳过，重试无意义。

**③ 换 token**

POST https://auth.openai.com/oauth/token

Content-Type: application/x-www-form-urlencoded

User-Agent: codex_cli_rs/0.146.0

originator: codex_cli_rs

grant_type=authorization_code

&client_id=app_EMoamEEZ73f0CkXaXp7hrann

&code={code}

&redirect_uri=http://localhost:1455/auth/callback

&code_verifier={verifier}

返回 access_token / refresh_token / id_token / expires_in / scope；offline_access 是拿到 refresh_token 的必需 scope。

### 2.3 工作区 / 多账号选择

POST https://auth.openai.com/api/accounts/workspace/select

{"workspace_id": "..."}

- 响应带 continue_url 时需 GET 跟随完成切换；

- 已登录态直调可能 409 invalid_state（需先请求 workspace 页初始化 state/cookie）。

POST https://auth.openai.com/api/accounts/session/select

{"session_id": "us\_..."}

- session_id 取自登录页注入的 unified session id。

## 3. 轮转（运行期）

### 3.1 权威成员快照

GET /backend-api/accounts/{workspace_id}/users?offset=0&limit=100&query=

- 用途：轮转的成员/水位权威数据源；多轮分页去重后 coverage=1 才可信（coverage\<1 不得作为踢人/分级依据）。

- 头：Authorization + Origin + Referer: https://chatgpt.com/admin/members + Sec-Fetch-\*。

### 3.2 席位类型计数

GET /backend-api/accounts/{workspace_id}/users/seat_type_counts

- 响应：{"seat_type_counts":{"default":N,"prolite":N,"usage_based":N,"automation":N}}（类别由服务端定义，可能出现新键）。

- 口径：**只是估算**，不能当权威成员数发布。

### 3.3 订阅与席位

GET /backend-api/subscriptions?account_id={workspace_id}

- 关键字段：id / plan_type / billing_period / seats_in_use / seats_entitled / will_renew / active_until。

- 备注：与 3.2 的席位计数在同一份快照里一起拉取。

### 3.4 账号总览（工作区清单 / 停用判定）

GET /backend-api/accounts/check/v4-2023-04-27?timezone_offset_min=-480

- 响应 accounts{} 每项含 account（plan_type / account_user_role / is_deactivated）与 entitlement（订阅）等。

### 3.5 已发邀请列表

GET /backend-api/accounts/{workspace_id}/invites?limit={n}&offset={n}

### 3.6 加入请求列表（admin members?tab=requests）

GET /backend-api/accounts/{workspace_id}/invites

?include_pending=false&include_requests=true&offset={n}&limit={n}&query=

- 额外特殊头（对齐浏览器抓包）：x-oai-is-client-observation、x-oai-is-pending-updates、x-openai-target-path、x-openai-target-route。

- 响应项：id / invite_id / email_address / role / seat_type / created_time。

### 3.7 退出 / 踢人

DELETE /backend-api/accounts/{workspace_id}/users/{user_id}

- 两种调用者：

- **子号自退**（轮转默认）：用子号自己的 workspace AT，限流天然分散到各账号。

- **母号踢人**（兜底）：用母号（owner）AT 直删，全部请求打同一账号，批量易 429/409。

- 头：Authorization + Chatgpt-Account-Id: {ws} + oai-device-id。

- 语义：

- 2xx 仍是**临时结果**，必须等一次精确成员快照复核"确实不在列表里"才算成功；

- 409 退避重试（attempt×5s → 5/10/15s，最多 3 次）；

- 401/403 视为 AT 失效 → 走 3.8 重登自退。

### 3.8 重登自退链路（AT 缺失或 401/403 时）

1. POST https://auth.openai.com/api/accounts/password/verify （账号密码，带哨兵请求）

2. POST https://auth.openai.com/api/accounts/mfa/verify （2FA 动态码 TOTP；见 2.2 变体）

3. PKCE 换目标空间 AT （见 2.3）

4. DELETE /backend-api/accounts/{ws}/users/{user_id} （用新 AT 自退）

- **必须用 GateOnly 变体**：账号密码 + 2FA（TOTP）通过即放行 session（personal AT 获取撞 CF 403 时降级），否则卡死在 final session fetch failed: \[Session\] HTTP 403。

- GateOnly 降级时拿不到 Info → **userID 必须由调用方显式传**，不能靠 session 推断。

### 3.9 上车（补池四步，含换 workspace token）

1. POST /backend-api/accounts/{ws}/invites/request （body {}）

2. POST /backend-api/accounts/{ws}/invites/accept （body {}）

3. GET /backend-api/accounts/check/v4-2023-04-27?timezone_offset_min=-480

4. GET /api/auth/session?exchange_workspace_token=true

&workspace_id={ws}&reason=setCurrentAccountWithoutRedirect

- 每步带 x-openai-target-path / x-openai-target-route，如：

- 第 1 步：/backend-api/accounts/{account_id}/invites/request

- 第 2 步：/backend-api/accounts/{account_id}/invites/accept

- 第 4 步：/api/auth/session

- 第 4 步返回 session JSON（accessToken 等），用于把当前会话切到目标空间。

## 4. 用量探测与满额判定

### 4.1 用量窗口（主判据）

GET https://chatgpt.com/backend-api/wham/usage

- 只读速率窗口，**不消耗配额**；解析出 5h/7d 两个窗口的已用百分比与重置秒数（pct_5h / pct_7d / reset_5h / reset_7d，无法归类的窗口记 unknown），用于账号分级。

- 头（对齐 Codex CLI 官方客户端）：Authorization: Bearer \<AT\> ---- Originator: codex_cli_rs（客户端来源）---- User-Agent: codex_cli_rs/\<ver\>（客户端标识）---- Version（客户端版本）；团队账号带 chatgpt-account-id（可从 AT 的 JWT 自动解析）。

- 满额（额度用尽）的判定字段与归并口径见 **4.7**。

- Codex OAuth 专用变体：头用官方 Codex 0.146.0 UA，不带 responses-beta/target 头。

### 4.2 身份元数据

GET https://chatgpt.com/backend-api/me

- 字段：phone_number / mfa_flag_enabled / email_domain_type / chatgpt_created / plan_type（仅在 wham 成功后补拉，失败静默）。

### 4.3 Codex PAT 身份校验

GET https://auth.openai.com/api/accounts/v1/user-auth-credential/whoami

- 用于 at-\* Codex PAT：PAT 不是 JWT，先取权威身份（chatgpt_account_id 等）再查用量。

### 4.4 Agent Identity 验活

POST https://chatgpt.com/backend-api/codex/responses

- 用 AgentAssertion 头验证 agent 身份存活。

### 4.5 出口地域预检

GET https://chatgpt.com/cdn-cgi/trace

- 返回 loc / colo（ChatGPT 自家 CDN 对出口 IP 的地域判定，比第三方 IP 库准）。

- 封禁地区（HK/CN/RU/KP/IR/SY/CU）会 403 HTML 拦截 → 会把活号误判成封禁，探测前先预检。

### 4.6 分级口径（唯一来源）

- 分级只有一个权威实现，禁止自写第二套阈值/窗口判定。

- 轮转探测要点：只用 **access_token** 探测，**绝不**用 refresh_token；失败号快速落 unknown，不让超时拖垮整轮。

### 4.7 满额（额度用尽）判定

「满额」不是独立接口，而是 GET /backend-api/wham/usage 返回的三类信号：

| 信号 | 来源 | 归并结果 |
| --- | --- | --- |
| HTTP 429 | — | status=limit_reached、error_code=rate_limited |
| 面板权威标志 | rate_limit.limit_reached=true | limit_reached；200 时提升为 status=limit_reached、error_code=usage_limit_reached |
| 面板长窗满 | pct_7d \>= 100 | 同上（7d/30d 都落在 pct_7d 键） |
| 面板允许位 | rate_limit.allowed=false | 同上 |
| 辅助字段（诊断展示） | credits.overage_limit_reached / spend_control.reached | overage_reached / spend_reached |

口径要点：

- 面板里的 limit_reached 才是**权威事实**（sub2api 同口径）；429 只是限流的一种表现形式——只判 429 会让用满的账号在库存页显示「可用 100%」。

- **5h 满 ≠ 满额**（2026-08-28 修正）：5h 短窗会自动重置，不能当终结态；只有长窗（7d/30d）满或远端 limit_reached 才代表额度真正耗尽。

- 401/403 **不是满额**：403 归 forbidden / oauth_usage_forbidden；401 归 auth_error（账号还活着，不判死）。

分级落地的判定顺序固定（阈值由调用方传入）：

1. pct_7d \>= threshold → exhausted + 7d（优先级最高）

2. pct_5h \>= threshold → exhausted + 5h

3. pct_unknown \>= threshold → exhausted + unknown（非 5h/7d 周期，临时退场）

4. limit_reached / allowed=false / overage_reached 但无达阈值窗口 → exhausted + unknown（临时退场，绝不冒充 7d）

5. 其余非 ok 状态 → error（不踢）

- 限额行 detail 必须保留 reason / quota_window / reset_after_seconds / pct\_\* / reset\_\* / limit_reached / allowed / overage_reached，不得压成裸 limit_reached。

- ⚠️ **轮转踢人侧口径差异**：踢拉循环刻意把 5h 满的号当 exhausted 踢出循环（业务边界不同）；库存/交付侧判满额则不看 5h。

## 5. sub2api 上传接口（轮转交付）

轮转产出的账号，由 aBai 经该端点上传后对外发布。

- Base：\<端点 BaseURL\>/api/v1（未带该后缀时自动补全）。

- 认证：X-Api-Key: \<api_key\>（401/403 归类为配置错误，不重试）。

- 响应包裹：{"code":0,"msg":"","data":...}。

| 动作 | 接口 | 备注 |
| --- | --- | --- |
| 创建账号（直传） | POST /admin/accounts | managed 版带 Idempotency-Key: rotation-create-\<request_id\>，写后必须回读确认 |
| 批量创建 | POST /admin/accounts/batch |  |
| 查重 | GET /admin/accounts?search={email}&page=1&page_size=100 |  |
| 分页拉取 | GET /admin/accounts?page={n}&page_size={n} |  |
| 读回单账号 | GET /admin/accounts/{id} | AccountSummary（状态 / 可调度 / 凭据回执） |
| 写入 OAuth 凭据 | POST /admin/accounts/{id}/apply-oauth-credentials | {"type":"oauth","credentials":{...}} |
| 刷新 | POST /admin/accounts/{id}/refresh |  |
| 批量更新 | POST /admin/accounts/bulk-update | managed 同步用 |
| 删除 | DELETE /admin/accounts/{id} |  |
| 分组列表 | GET /admin/groups |  |

轮转交付硬约束（代码里的一等公民）：

- **\`model_mapping: {}\`**：推送账号不带模型映射（硬不变量）。

- 账号名固定 rotation-\<binding_id\>；写入前校验 generation（防止身份漂移/陈旧凭据覆盖）。

- managed 写入后必须回读账号摘要（AccountSummary），校验 generation 与远端凭据齐全：access_token（AT）---- refresh_token（RT）---- id_token。

- **远端满额/限流的回读口径**：AccountSummary 带 rate_limit_reset_at；写入 apply-oauth-credentials 前若 rate_limit_reset_at / overload_until / temp_unschedulable_until 任一仍在未来，写入直接被判 remote_rate_limited，需等窗口过期再推。

## 6. 链路总览

![邀请、OAuth、轮转与交付链路总览](rotation-接口与链路总览.media/media/image1.png)

## 7. 阶段 / 环节 → 接口对照

| 阶段 / 环节 | 使用接口 |
| --- | --- |
| 邀请：批量邀请（群发邀请邮件） | POST .../invites（owner workspace AT，25/批，队列重试 + 5 分钟硬截断） |
| 邀请：邀请维护（清死号 / 补邀请） | POST / DELETE / PATCH .../invites |
| OAuth：取会话 | GET /api/auth/session |
| OAuth：登录 | password/verify → mfa/verify → client_auth_session_dump |
| OAuth：授权换 token | GET /oauth/authorize → POST /oauth/token |
| OAuth：工作区 / 多账号选择 | workspace/select · session/select |
| 轮转：成员 / 水位快照 | GET .../users |
| 轮转：席位 / 订阅核对 | GET .../users/seat_type_counts、GET .../subscriptions |
| 轮转：账号分级 / 用量检测（含满额） | GET .../wham/usage（4.1、4.7）、GET .../me（+4.3/4.4/4.5） |
| 轮转：踢人 | DELETE .../users/{user_id}（子号自退 / 母号兜底） |
| 轮转：重登自退 | password/verify → mfa/verify → PKCE → DELETE |
| 轮转：补池上车 | invites/request → invites/accept → accounts/check → api/auth/session 交换 |
| 交付推送 | POST /api/v1/admin/accounts + apply-oauth-credentials + 回读确认 |

## 8. 坑位速查

- 409 ≠ 权限拒绝，退避可过；401/403 才是 AT 问题。

- 2xx 删除/写入都是**临时结果**，必须用后续精确快照/回读确认。

- 邀请接口限流敏感：per-workspace 429 + 触发 CF 403；维护最小间隔 10 分钟。

- 团队接口漏带 chatgpt-account-id 或浏览器语义头 → 403 HTML，排查时先看响应是不是 HTML。

- 探测链路全程只用 AT（access_token），不用 RT（refresh_token）；出口落在封禁地区会被 CF 拦成"假死"。

- 「满额」以 wham 面板 limit_reached（或 pct_7d 满）为权威，**5h 满不算**；踢人侧口径不同（5h 满也退场）——引用满额结论前先对口径。

- sub2api 写入全部走 Idempotency-Key + generation 校验，禁止绕过 managed 路径裸写。
