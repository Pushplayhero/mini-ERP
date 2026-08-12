# Mini-ERP 架構藍圖（Order-to-Cash）

> 定位：一個範圍極小、工程品質極高的開源 ERP 核心，展示後端工程能力。
> 技術棧：Python 3.12 + FastAPI + SQLAlchemy 2.0 + PostgreSQL
> 時程：8 週衝刺
> 建議 repo 名：`ledgerflow` / `ordercore` / `mini-erp`（英文 README，國際可見度）

---

## 1. 範圍（v0.1 只做這條線）

```
客戶/商品主檔 → 銷售訂單 → 出貨（扣庫存）→ 應收發票 → 收款 → 總帳分錄（自動過帳）
```

**明確不做**（寫進 README 的 Non-Goals，這本身就是加分項）：
採購、生產、人事、多公司、多幣別、審批流、前端 UI。

## 2. 架構風格：Modular Monolith

單一部署單元，內部按領域切五個模組，模組間禁止跨模組 import 對方的
model/repository，只能走 service 介面或領域事件。

```
app/
├── modules/
│   ├── masterdata/    # 客戶、商品、會計科目
│   ├── sales/         # 銷售訂單（生命週期：draft → confirmed → shipped → closed）
│   ├── inventory/     # 庫存異動帳（append-only ledger，非直接 update 數量欄位）
│   ├── receivables/   # 發票、收款、沖帳
│   └── ledger/        # 總帳：分錄、帳期、試算表
│   └── <module>/
│       ├── router.py      # FastAPI 路由（薄）
│       ├── service.py     # 業務邏輯 + 交易邊界
│       ├── models.py      # SQLAlchemy models（模組私有）
│       ├── schemas.py     # Pydantic DTO
│       └── events.py      # 發布的領域事件定義
├── core/
│   ├── db.py              # session 管理、Unit of Work
│   ├── events.py          # 同步 in-process 事件匯流排
│   ├── posting.py         # 過帳引擎（見 §4）
│   └── exceptions.py
└── main.py
```

**面試講點**：為什麼不用微服務——O2C 流程需要跨模組強一致
（出貨要同時扣庫存、開應收、記帳，一個 DB transaction 完成），
用 import-linter 在 CI 強制模組邊界，得到微服務的紀律、免掉分散式事務的成本。

## 3. 核心資料模型

```
customers(id, code, name, credit_limit, is_active)
products(id, sku, name, unit_price, is_active)
accounts(id, code, name, type[asset|liability|equity|revenue|expense])

sales_orders(id, order_no, customer_id, status, total, snapshot_*)   -- 快照客戶名/價格
sales_order_lines(id, order_id, product_id, qty, unit_price, snapshot_sku)

stock_moves(id, product_id, qty_delta, move_type[shipment|adjustment], ref_type, ref_id, created_at)
  -- append-only：現有庫存 = SUM(qty_delta)，可加 materialized view 加速

invoices(id, invoice_no, customer_id, order_id, amount, status[open|partial|paid])
payments(id, payment_no, customer_id, amount, received_at)
payment_allocations(id, payment_id, invoice_id, amount)   -- 一筆收款可沖多張發票

journal_entries(id, entry_no, entry_date, period_id, source_type, source_id, posted_at)
journal_lines(id, entry_id, account_id, debit, credit)    -- CHECK: debit*credit=0
accounting_periods(id, year, month, status[open|closed])
```

三個刻意展示的設計決策：
1. **交易快照主檔值**（訂單存下單當時的價格與客戶名）— 改主檔不影響歷史單據。
2. **庫存用異動帳而非數量欄位** — 數量永不被覆寫、天然稽核軌跡；「庫存 ≥ 0」
   不變量另以 per-product summary row + `SELECT FOR UPDATE` 保證（見 master plan §10）。
3. **分錄不可變** — 沒有 UPDATE/DELETE endpoint，錯帳開反向分錄沖銷。

## 4. 過帳引擎（專案的技術亮點）

業務模組不直接寫分錄。它們發布領域事件，過帳引擎依「過帳規則表」把事件轉成分錄：

```python
# core/posting.py — 宣告式規則
POSTING_RULES = {
    "goods_shipped": [
        Rule(debit="5000-COGS",           credit="1300-Inventory",  amount="cost"),
    ],
    "invoice_issued": [
        Rule(debit="1100-AR",             credit="4000-Revenue",    amount="net"),
    ],
    "payment_received": [
        Rule(debit="1000-Cash",           credit="1100-AR",         amount="amount"),
    ],
}
```

不變量（用測試證明）：
- 每張分錄借貸必平（DB constraint + property-based test）。
- 事件與分錄同一個 transaction — 不會有出了貨沒記帳的狀態。
- 帳期已關閉（`periods.status = closed`）則拒絕過帳。

## 5. API 面（REST，OpenAPI 自動生成）

```
POST /sales-orders                 建立訂單（檢查信用額度）
POST /sales-orders/{id}/confirm    確認（鎖定價格）
POST /sales-orders/{id}/ship       出貨（扣庫存 + COGS 分錄，庫存不足回 409）
POST /invoices                     由訂單開立發票（+ AR/Revenue 分錄）
POST /payments                     收款 + 沖帳（+ Cash/AR 分錄）
GET  /reports/trial-balance        試算表（借貸總額必相等 = 系統自證正確）
GET  /reports/ar-aging             應收帳齡
GET  /journal-entries?source=...   任何單據可追溯到分錄（審計軌跡）
```

`trial-balance` 是 demo 殺手鐧：跑完一輪 O2C，試算表借貸平衡，證明整個系統的正確性。

## 6. 工程品質配置（這部分決定履歷價值）

| 項目 | 選擇 |
|---|---|
| 測試 | pytest + testcontainers（真 PostgreSQL）；單元 + API 整合 + 一條完整 O2C E2E |
| Property-based | hypothesis：任意合法事件序列 → 試算表恆平衡、庫存恆 ≥ 0 |
| Lint/型別 | ruff + mypy --strict |
| 模組邊界 | import-linter（CI 強制，違反邊界 build fail） |
| Migration | Alembic |
| CI | GitHub Actions：lint → mypy → test → coverage badge |
| 部署 | docker compose up 一鍵起（app + postgres + seed data） |
| 文件 | README（英文）+ docs/adr/*.md + C4 架構圖（mermaid） |

**ADR 至少寫四篇**（面試官真的會看）：
- ADR-001 為什麼選 modular monolith 而非微服務
- ADR-002 庫存採 append-only 異動帳
- ADR-003 過帳引擎：宣告式規則 vs 硬編碼
- ADR-004 同步 in-process 事件 vs message queue

## 7. 8 週路線圖

| 週 | 交付 |
|---|---|
| 1 | Repo 骨架、CI、docker compose、masterdata CRUD、Alembic |
| 2 | ledger 模組：科目、分錄、帳期、試算表 API |
| 3 | 過帳引擎 + 事件匯流排 + property-based tests |
| 4 | sales 模組：訂單生命週期、信用額度檢查 |
| 5 | inventory：異動帳、出貨扣庫存、COGS 過帳 |
| 6 | receivables：發票、收款、沖帳、帳齡報表 |
| 7 | E2E 測試、seed demo data、效能小調（報表 query） |
| 8 | README 打磨、ADR、mermaid 架構圖、demo GIF、v0.1.0 release tag |

原則：**每週結束時 main branch 都是可 demo 狀態**。第 8 週的打磨不能省 —
README 的品質決定了前 30 秒印象。

## 8. README 賣點清單（放最上面）

- 一句話定位：*A minimal, correctness-obsessed ERP core demonstrating
  double-entry bookkeeping, event-driven posting, and modular monolith architecture.*
- Demo GIF：curl 打完一輪 O2C → trial balance 平衡
- 架構圖（mermaid C4）
- "Design Decisions" 段落連到 ADR
- Non-Goals 段落（展示範圍紀律）
- Quick start：`docker compose up` → `make seed` → `make demo`

## 9. 履歷/面試敘事

> 「我做了一個開源 mini-ERP，實作 order-to-cash 全流程。技術重點是一個宣告式
> 過帳引擎，把領域事件轉成複式簿記分錄，用 property-based testing 證明任意
> 事件序列下試算表恆平衡。架構是 modular monolith，用 import-linter 在 CI
> 強制模組邊界。」

這段話涵蓋：領域知識、正確性思維、測試深度、架構取捨 — 四個後端面試核心維度。
