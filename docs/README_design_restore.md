# コードからシステム設計を還元するツールチェーン

GeneXus生成Javaコードとデータベースから、日本語のシステム設計書を自動生成します。

## 全体フロー

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           入力ソース                                     │
├───────────────┬───────────────┬───────────────────────────────────────────┤
│               │               │                                           │
│  Java ソース   │   DDLファイル  │   GeneXus KB（オプション）                 │
│  (GeneXus生成) │  (.sql)       │                                           │
│               │               │                                           │
└───────┬───────┴───────┬───────┴───────────────────────────────────────────┘
        │               │
        ▼               ▼
┌───────────────┐ ┌───────────────┐
│ Step 1        │ │ Step 2        │
│ parse_genexus │ │ extract_db_   │
│ .py           │ │ metadata.py   │
│               │ │ (DDL解析)     │
└───────┬───────┘ └───────┬───────┘
        │               │
        ▼               ▼
  java_structure   db_metadata
     .json            .json
        │               │
        └───────┬───────┘
                │
                ▼
        ┌───────────────┐
        │ Step 3        │
        │ analyze_code_ │
        │ db_mapping.py │
        └───────┬───────┘
                │
                ▼
         design_document
            .json
                │
                ▼
        ┌───────────────┐
        │ Step 4        │
        │ ai_design_    │
        │ restorer.py   │
        └───────┬───────┘
                │
        ┌───────┴───────┐
        │               │
        ▼               ▼
  final_design    設計書.md
     .json       (Markdown)
```

## インストール

```bash
# 必須依存関係
pip install tree-sitter tree-sitter-java

# AI機能（オプション）
pip install anthropic
```

**注意**: データベース接続は不要です。DDLファイル（.sql）から直接メタデータを抽出します。

## 使用方法

### Step 1: Javaコード解析

```bash
# GeneXus生成コードを解析
python parse_genexus.py /path/to/java/project -o java_structure.json

# 出力例:
# [情報] 機能タイプ統計: 画面=45, バッチ=28, その他=120
# [情報] GeneXusタイプ統計: {'WebPanel': 30, 'Transaction': 15, 'Procedure': 20, ...}
```

### Step 2: データベースメタデータ抽出（DDLファイルから）

```bash
# DDLファイルからメタデータを抽出（方言自動検出）
python extract_db_metadata.py schema.sql -o db_metadata.json

# MySQL/MariaDB DDL
python extract_db_metadata.py schema.sql -o db_metadata.json --dialect mysql

# Oracle DDL（COMMENT ON TABLE/COLUMN対応）
python extract_db_metadata.py schema.sql -o db_metadata.json --dialect oracle

# SQL Server DDL（sp_addextendedproperty対応）
python extract_db_metadata.py schema.sql -o db_metadata.json --dialect sqlserver

# PostgreSQL DDL（COMMENT ON対応）
python extract_db_metadata.py schema.sql -o db_metadata.json --dialect postgresql

# Markdown形式のスキーマ定義書も同時出力
python extract_db_metadata.py schema.sql -o db_metadata.json --md schema_doc.md
```

**対応するコメント形式:**

| データベース | コメント形式 |
|-------------|-------------|
| MySQL | `COMMENT '日本語名'` (テーブル/カラム) |
| Oracle | `COMMENT ON TABLE/COLUMN ... IS '日本語名'` |
| SQL Server | `sp_addextendedproperty @name='MS_Description'` |
| PostgreSQL | `COMMENT ON TABLE/COLUMN ... IS '日本語名'` |

### Step 3: コード-DB関連分析

```bash
python analyze_code_db_mapping.py \
    java_structure.json \
    db_metadata.json \
    -o design_document.json

# 出力例:
# ======================================================================
# システム設計還元サマリー
# ======================================================================
# 
# 【統計情報】
#   機能数: 73
#     - 画面機能: 45
#     - バッチ機能: 28
#   テーブル数: 85
#   リレーション数: 42
```

### Step 4: AI推論と設計書生成

```bash
# ルールベース推論（API不要）
python ai_design_restorer.py \
    design_document.json \
    -o final_design.json \
    --markdown 設計書.md \
    --no-ai

# Claude API使用（より高精度）
export ANTHROPIC_API_KEY=your_api_key
python ai_design_restorer.py \
    design_document.json \
    -o final_design.json \
    --markdown 設計書.md
```

## 出力ファイル説明

### java_structure.json

```json
{
  "project_root": "/path/to/project",
  "file_count": 200,
  "function_type_stats": {"screen": 45, "batch": 28, "other": 127},
  "genexus_type_stats": {"WebPanel": 30, "Transaction": 15, ...},
  "files": [
    {
      "file": "customer_trn.java",
      "package": "com.example.trn",
      "function_type": "screen",
      "classes": [
        {
          "name": "customer_trn",
          "function_type": "screen",
          "genexus_type": "Transaction",
          "methods": [...],
          "dependencies": {...}
        }
      ]
    }
  ]
}
```

### db_metadata.json

```json
{
  "source_file": "schema.sql",
  "dialect": "mysql",
  "database": "",
  "table_count": 85,
  "column_count": 650,
  "tables": [
    {
      "schema_name": "",
      "table_name": "Customer",
      "logical_name": "得意先マスタ",
      "table_type": "TABLE",
      "comment": "得意先マスタ - 取引先情報を管理",
      "columns": [
        {
          "name": "CustomerId",
          "logical_name": "得意先ID",
          "data_type": "INT",
          "length": null,
          "precision": null,
          "scale": null,
          "nullable": false,
          "is_primary_key": true,
          "is_foreign_key": false,
          "foreign_key_table": null,
          "default_value": null,
          "comment": "得意先ID - 主キー"
        }
      ],
      "row_count": null
    }
  ],
  "foreign_keys": [
    {
      "constraint_name": "FK_Order_Customer",
      "from_table": "Order",
      "from_columns": ["CustomerId"],
      "to_table": "Customer",
      "to_columns": ["CustomerId"]
    }
  ]
}
```

### design_document.json（最終設計）

```json
{
  "project_name": "/path/to/project",
  "statistics": {
    "total_functions": 73,
    "screen_functions": 45,
    "batch_functions": 28,
    "total_tables": 85
  },
  "ai_analysis": {
    "system_overview": "本システムは販売管理、在庫管理、マスタ管理を統合的に管理する業務システムです。",
    "business_domains": ["販売管理", "在庫管理", "マスタ管理"]
  },
  "functions": [
    {
      "id": "customer_trn",
      "name": "得意先登録画面",
      "type": "screen",
      "genexus_type": "Transaction",
      "description": "対象テーブル: 得意先マスタ | 操作: 登録/参照/更新/削除",
      "business_intent": "得意先マスタに関するデータ登録",
      "tables": [
        {
          "table_name": "Customer",
          "logical_name": "得意先マスタ",
          "operations": ["SELECT", "INSERT", "UPDATE", "DELETE"]
        }
      ],
      "crud_matrix": {
        "CREATE": ["Customer"],
        "READ": ["Customer"],
        "UPDATE": ["Customer"],
        "DELETE": ["Customer"]
      }
    }
  ],
  "table_function_matrix": {
    "Customer": ["customer_trn", "customer_list_wp", "customer_report_proc"]
  },
  "er_diagram": {
    "tables": [...],
    "relationships": [...]
  }
}
```

### 設計書.md（Markdown設計書）

```markdown
# システム設計書

## 1. システム概要

本システムは販売管理、在庫管理、マスタ管理を統合的に管理する業務システムです。

### 業務ドメイン
- 販売管理
- 在庫管理
- マスタ管理

## 2. 統計情報

| 項目 | 件数 |
|------|------|
| 機能総数 | 73 |
| 画面機能 | 45 |
| バッチ機能 | 28 |
| テーブル数 | 85 |

## 3. 機能一覧

### 3.1 画面機能

| 機能ID | 機能名 | GXタイプ | 使用テーブル | 目的 |
|--------|--------|----------|--------------|------|
| customer_trn | 得意先登録画面 | Transaction | 得意先マスタ | 得意先マスタに関するデータ登録 |
...
```

## 高度な使用法

### 複数プロジェクトの比較

```bash
# プロジェクトA
python parse_genexus.py /project_a -o java_structure_a.json
python analyze_code_db_mapping.py java_structure_a.json db_metadata.json -o design_a.json

# プロジェクトB
python parse_genexus.py /project_b -o java_structure_b.json
python analyze_code_db_mapping.py java_structure_b.json db_metadata.json -o design_b.json

# 差分分析（カスタムスクリプトで）
python compare_designs.py design_a.json design_b.json
```

### DDLファイルでのコメント記述例

日本語名を正しく抽出するために、DDLファイルに以下の形式でコメントを記述してください：

```sql
-- MySQL/MariaDB
CREATE TABLE Customer (
    CustomerId INT PRIMARY KEY COMMENT '得意先ID',
    CustomerName VARCHAR(100) NOT NULL COMMENT '得意先名称',
    CustomerEmail VARCHAR(200) COMMENT 'メールアドレス'
) COMMENT='得意先マスタ';

-- Oracle
CREATE TABLE Customer (
    CustomerId NUMBER(10) PRIMARY KEY,
    CustomerName VARCHAR2(100) NOT NULL,
    CustomerEmail VARCHAR2(200)
);
COMMENT ON TABLE Customer IS '得意先マスタ';
COMMENT ON COLUMN Customer.CustomerId IS '得意先ID';
COMMENT ON COLUMN Customer.CustomerName IS '得意先名称';
COMMENT ON COLUMN Customer.CustomerEmail IS 'メールアドレス';

-- SQL Server
CREATE TABLE [dbo].[Customer] (
    [CustomerId] INT PRIMARY KEY,
    [CustomerName] NVARCHAR(100) NOT NULL,
    [CustomerEmail] NVARCHAR(200)
);
EXEC sp_addextendedproperty 
    @name = N'MS_Description', @value = N'得意先マスタ',
    @level0type = N'SCHEMA', @level0name = N'dbo',
    @level1type = N'TABLE', @level1name = N'Customer';
EXEC sp_addextendedproperty 
    @name = N'MS_Description', @value = N'得意先ID',
    @level0type = N'SCHEMA', @level0name = N'dbo',
    @level1type = N'TABLE', @level1name = N'Customer',
    @level2type = N'COLUMN', @level2name = N'CustomerId';

-- PostgreSQL
CREATE TABLE customer (
    customer_id SERIAL PRIMARY KEY,
    customer_name VARCHAR(100) NOT NULL,
    customer_email VARCHAR(200)
);
COMMENT ON TABLE customer IS '得意先マスタ';
COMMENT ON COLUMN customer.customer_id IS '得意先ID';
COMMENT ON COLUMN customer.customer_name IS '得意先名称';
COMMENT ON COLUMN customer.customer_email IS 'メールアドレス';
```

### カスタム辞書の追加

`ai_design_restorer.py` の `TERM_DICTIONARY` にプロジェクト固有の用語を追加：

```python
TERM_DICTIONARY = {
    # 既存の辞書...
    
    # プロジェクト固有の用語
    'myterm': '自社用語',
    'abc': 'ABC機能',
}
```

## ファイル一覧

| ファイル | 説明 |
|---------|------|
| `parse_genexus.py` | GeneXus生成Javaコードの解析 |
| `extract_db_metadata.py` | データベースメタデータ抽出 |
| `analyze_code_db_mapping.py` | コード-DB関連分析 |
| `ai_design_restorer.py` | AI推論と設計書生成 |
| `function_type_config_genexus.json` | GeneXus用設定ファイル |

## トラブルシューティング

### テーブル名が検出されない

- データベースメタデータが正しく抽出されているか確認
- 大文字/小文字の違いを確認（SQLServerは大文字小文字を区別しない場合あり）

### 日本語名が表示されない

- データベースにコメントが設定されているか確認
- コメントの形式が正しいか確認（「論理名: 説明」形式を推奨）

### AI推論がうまくいかない

- Claude API Keyが正しく設定されているか確認
- `--no-ai` オプションでルールベース推論を使用
