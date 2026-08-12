# 開源 ERP 作戰計畫 — 對標鼎新

> 願景：open-source、API-first、為台灣中小製造業設計的現代 ERP。
> 定位：不是做「功能更多的鼎新」，是做「鼎新做不到的鼎新」——
> 開放、可自架、客製靠寫 plugin 而非買顧問時數。
> 技術棧：Python 3.12 + FastAPI + SQLAlchemy 2.0 + PostgreSQL

---

## 0. 對手分析：鼎新的強弱點

| | 鼎新（Workflow/TipTop/T100） | 我們的打法 |
|---|---|---|
| 功能深度 | 30 年累積，追不上 | 不追。只做 20% 高頻功能做到極好 |
| 產業 know-how | 導入顧問網絡強 | 開源社群 + 文件取代顧問 |
| 技術棧 | 4GL/老架構、封閉 | API-first、現代 DX、docker 一鍵起 |
| 客製 | 改 code、貴、升級卡死 | Plugin 架構：客製不碰核心，升級無痛 |
| 授權費 | 高、綁年約 | 開源免費，商模留給未來（託管/支援） |
| 台灣財稅 | 完整（電子發票、營業稅） | Phase 4 重點投資——這是 Odoo/ERPNext 打不進來的護城河 |

結論：戰場不在功能清單，在**架構世代差**與**開放性**。

## 1. 總體策略：核心 → 平台 → 套件 → 本地化

每個 Phase 結束都是一個可用、可 demo、可吸引貢獻者的 release。
絕不同時開兩個 Phase。

```
Phase 1  Kernel      (0–3 月)   O2C 一條線 + 總帳，證明核心正確性
Phase 2  Platform    (3–7 月)   讓別人能在上面蓋東西：plugin、自訂欄位、工作流
Phase 3  Suites      (7–14 月)  採購線 P2P、MRP 生產、成本計算
Phase 4  台灣本地化   (14 月+)   電子發票（財政部 API）、營業稅 401 申報、IFRS 報表
```

## 2. 地基決策 — 現在不做對，以後補不了

這是本次「做大」相對原 mini 計畫真正的差異。程式碼量 Phase 1 幾乎不變，
變的是 schema 與核心抽象：

**2.1 多公司**：所有業務表都帶 `company_id`，權限與查詢層強制過濾。
單公司使用者無感，但多法人是 ERP 的入場券。

**2.2 多幣別**：金額欄位三件套 `amount / currency_code / functional_amount`
（交易幣別 + 本位幣），匯率表含生效日。Phase 1 可以只支援 TWD，但 schema 先到位。

**2.3 計量單位（UoM）**：`qty + uom_id` + 換算表。製造業沒有 UoM 寸步難行，
事後加要改所有交易表。

**2.4 自訂欄位**：每張業務表帶 `custom_data JSONB` + 中央欄位定義表
（型別、驗證、顯示標籤）。這是「客製不改核心」的第一根柱子。

**2.5 Plugin 架構（Odoo 真正的護城河，也是我們的）**：
- 核心定義 hook points：`before_confirm / after_post / validate_*`
- Plugin 是獨立 Python package，透過 entry points 註冊
- Plugin 可以：加 API route、訂閱領域事件、註冊過帳規則、擴充驗證
- Phase 1 就用這機制實作一個官方 plugin（如「信用額度檢查」）自我驗證

**2.6 事件模型**：Phase 1 用同步 in-process event bus（交易一致性優先），
但落地 **outbox pattern**——事件同交易寫入 `outbox` 表。Phase 2+ 要接
message queue、webhook、非同步整合時，直接從 outbox 讀，核心不用改。

**2.7 i18n**：所有 user-facing 字串（含錯誤訊息、欄位標籤）走 message catalog，
主檔支援多語名稱表。中英雙語 day 1。

**2.8 審計**：`created_by / updated_at` 之外，關鍵表全量 audit log
（誰、何時、改前改後）。台灣企業導入的第一個問題就是稽核。

## 3. 完整模組地圖

```
─── Phase 1: Kernel ───────────────────────────────
masterdata    客戶、供應商、商品、UoM、科目、幣別/匯率
ledger        分錄、帳期、試算表、過帳引擎（宣告式規則）
sales         訂單生命週期、信用額度（以 plugin 實作）
inventory     append-only 異動帳、出貨扣庫存
receivables   發票、收款、沖帳、帳齡

─── Phase 2: Platform ─────────────────────────────
platform.plugins      plugin loader、hook registry、相依管理
platform.customfields 自訂欄位引擎（定義、驗證、API 透出）
platform.workflow     審批流引擎（狀態機 + 審批鏈設定）
platform.permissions  RBAC + row-level（公司/部門/金額門檻）+ 欄位級
platform.integration  outbox → webhook、批次匯入匯出（Excel 是台灣企業的血液）

─── Phase 3: Suites ───────────────────────────────
purchase      請購 → 採購單 → 收料 → 應付（P2P 全線）
payables      應付發票、付款、沖帳
mrp           BOM（多階）、MPS/MRP 展算、工單、領退料
costing       標準成本 + 月加權平均、工單成本歸集、差異分析

─── Phase 4: 台灣本地化（獨立 plugin 套件包）────────
tw.einvoice   財政部電子發票整合（B2B/B2C、字軌管理）
tw.tax        營業稅 401 申報媒體檔、進項扣抵
tw.reports    IFRS/商業會計法報表格式
```

本地化做成 plugin 而非寫進核心——這同時證明 Phase 2 平台層是真的能用的。

## 4. 架構風格演進

**Phase 1–3 都是 modular monolith**，不動搖。理由：ERP 核心交易需要跨模組
強一致；一人開發，微服務是自殺。

允許拆出去的只有三類（Phase 3+，經由 outbox 事件驅動）：
報表/BI 查詢層（讀取副本）、整合閘道（webhook/對外同步）、排程批次（MRP 展算）。

CI 用 import-linter 強制模組邊界，違反即 fail——這讓 monolith 保持「隨時可拆」
的紀律，也是面試/README 的核心論述。

## 5. 開源經營（做大就必須做這塊）

- **License**：Apache-2.0（對企業採用最友善；等有商模再考慮 open-core）
- **治理**：ROADMAP.md 公開、ADR 全公開、CONTRIBUTING.md + good-first-issue 標籤
- **每 Phase 的社群鉤子**：
  - P1 出手時要有：英文 README + demo GIF + `docker compose up` 一鍵體驗
  - P2 是招募貢獻者的關鍵——「寫一個 plugin」是完美的 first contribution
  - P4 找台灣會計師/記帳士社群回饋規格
- **展示面**：GitHub Pages 放文件（mkdocs-material）、每月 release notes、
  可以的話架一個公開 demo 站（seed 假資料，每小時 reset）

## 6. 職涯槓桿（順便，但別忘了初衷）

- 每個 Phase 完成都是一條獨立的履歷 bullet，不用等全部做完
- P1 = 「複式簿記核心 + property-based testing」
- P2 = 「plugin 架構 + 工作流引擎」——這是 senior 級的系統設計題材
- 部落格化：每篇 ADR 展開成技術文章（過帳引擎設計、outbox pattern 實戰），
  文章帶流量 → star → 職涯機會的飛輪

## 7. 風險與紀律

| 風險 | 對策 |
|---|---|
| 燒完熱情（最大風險） | Phase gate：每 phase 有明確 DoD，完成才開下一個；每週 main 可 demo |
| 範圍蔓延 | Non-Goals 寫進 README；新想法一律進 backlog 不進當期 |
| 一人專案巴士係數 | 文件先行：ADR + 模組 README，讓任何人能接手 |
| 功能面被比較 | 敘事永遠強調架構與開放性，不跟鼎新比功能清單 |

## 8. 第一步不變

Phase 1 = 原 mini-ERP 藍圖（`mini-erp-architecture.md`），差異只有：
schema 依 §2 加上 company_id、幣別三件套、UoM、custom_data、outbox 表，
並把信用額度檢查改用 plugin hook 實作。

下一動作：對本計畫跑 /CODEX REVIEW ARCHITECTURE，通過後開 repo、動工 Week 1。

---

## 10. 審查修訂記錄（Consensus Review v1，2026-08-13）

以下修訂解決架構共識審查的五項 P2 發現，為 Phase 1 實作的規範性依據：

**10.1 金額精度與匯率政策**（解決：多幣別 schema 不完整）
- 所有金額欄位 `NUMERIC(20, 6)`；各幣別顯示位數由 `currencies.decimal_places` 決定
- 交易表存 `exchange_rate NUMERIC(20, 10)` + `rate_date`——歷史金額可重現，不回查匯率表
- `journal_lines` 同時帶交易幣別金額與本位幣金額；**借貸平衡以本位幣強制**（DB constraint）
- 捨入：line 層級 round-half-even；header 金額 = 已捨入 lines 之和（絕不重算）

**10.2 多公司隔離機制**（解決：僅宣告未定義機制）
- App 層強制：SQLAlchemy `with_loader_criteria` 全域 filter，company context 來自 request scope，
  未設定 context 的查詢直接拋錯（fail-closed）
- 專屬隔離測試套件：每個 API endpoint 都有跨公司存取必須 404/403 的測試
- Postgres RLS 列為 Phase 2 縱深防禦，非 Phase 1 依賴

**10.3 Plugin 信任模型與交易語意**（解決：未定義信任邊界）
- 信任模型明文化：plugin 為管理者安裝的受信任碼，in-process 執行，不做沙箱（同 Odoo 模式），寫入 SECURITY.md
- Hook 在業務交易內執行；hook 拋例外 = 整筆交易回滾（fail-closed），不吞例外
- Hook API 走 semver；**Phase 1 只做最小 hook registry**（validate / before / after 三類），
  完整 plugin loader、相依管理、entry-points 探索移至 Phase 2

**10.4 Outbox 投遞語意**（解決：只定義寫入端）
- Schema：`outbox(id UUID, event_type, payload JSONB, occurred_at, dispatched_at NULL, attempts INT)`
- 語意：at-least-once；消費端以 event id 冪等去重；指數退避重試；已投遞 30 天後清理
- Phase 1 交付：寫入 + replay CLI（驗證可讀性）；dispatcher 常駐程序是 Phase 2

**10.5 庫存併發控制**（解決：≥0 不變量無鎖保證）
- `stock_summary(product_id, company_id, on_hand)` 每 product 一列，與 moves 同交易維護
- 出貨時 `SELECT ... FOR UPDATE` summary row → 檢查 → 寫 move + 更新 summary
- moves 仍為唯一事實來源；summary 可隨時由 SUM 重建（提供 rebuild command + 對帳測試）
- 驗收：並發出貨整合測試（兩 session 同時搶最後一件，恰一成功）

**P3 延後記錄**：custom_data 索引與驗證策略（Phase 2 設計時定）；Phase 4 電子發票
憑證/個資需獨立安全審查；Phase 1 DoD 逐條清單見實作 brief。
