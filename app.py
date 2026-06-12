import gradio as gr
from openai import OpenAI
import json
import base64
import io
import os
import pandas as pd
from pypdf import PdfReader
import tempfile

# =========================================================================
# 🚨 【超重要】最新FastAPIとの相性バグを強制破壊するパッチ（最上部に配置）
# =========================================================================
import gradio_client.utils
orig_json_schema_to_python_type = gradio_client.utils._json_schema_to_python_type
def patched_json_schema_to_python_type(schema, defs=None):
    if isinstance(schema, bool):  # 原因だった「真偽値（bool）」が来たら安全にスルーさせる
        return "any"
    return orig_json_schema_to_python_type(schema, defs)
gradio_client.utils._json_schema_to_python_type = patched_json_schema_to_python_type

# =========================================================================
# 🔒 2. システム起動
# =========================================================================
print("🔒 システム起動中... (Renderプロモード稼働)")
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')

# =========================================================================
# ⚙️ 3. メインデータ処理エンジン
# =========================================================================
def safe_create_csvs(df):
    tmp_dir = tempfile.gettempdir()
    p1 = os.path.join(tmp_dir, "1_パソコン閲覧用_UTF8.csv")
    p2 = os.path.join(tmp_dir, "2_システム取込用_ShiftJIS.csv")
    df.to_csv(p1, index=False, encoding='utf-8-sig')
    df.to_csv(p2, index=False, encoding='cp932', errors='replace')
    return p1, p2

def process_webhook_app(uploaded_file, custom_cols_str):
    if uploaded_file is None: 
        return pd.DataFrame([{"システムメッセージ": "ファイルが選択されていません。"}]), None, None
    
    if not OPENAI_API_KEY:
        return pd.DataFrame([{"システムメッセージ": "Renderの環境変数（OPENAI_API_KEY）が設定されていません。"}], columns=["システムメッセージ"]), None, None
        
    desired_columns = [c.strip() for c in custom_cols_str.split(',') if c.strip()]
    
    try:
        file_path = uploaded_file if isinstance(uploaded_file, str) else uploaded_file.name
        file_ext = os.path.splitext(file_path)[1].lower()
        with open(file_path, "rb") as f: file_bytes = f.read()
    except Exception as e:
        return pd.DataFrame([{"システムメッセージ": f"読込エラー: {e}"}]), None, None

    final_orders = []

    # 📸 画像 / PDF の場合
    if file_ext in ['.jpg', '.jpeg', '.png', '.pdf']:
        client = OpenAI(api_key=OPENAI_API_KEY.strip())
        prompt = f"""
        あなたはデータ入力担当です。発注書から抽出し、以下のJSONで出力してください。
        {{ "data": [ {{ "作品名": "...", "部数": "..." }} ] }}
        【必須の抽出項目キー】: {desired_columns}
        """
        try:
            if file_ext in ['.jpg', '.jpeg', '.png']:
                base64_img = base64.b64encode(file_bytes).decode('utf-8')
                response = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": [{"type": "text", "text": prompt},{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_img}"}}]}],
                    response_format={ "type": "json_object" }
                )
            else:
                pdf_text = "".join([page.extract_text() + "\n" for page in PdfReader(io.BytesIO(file_bytes)).pages])
                response = client.chat.completions.create(
                    model="gpt-4o-mini", 
                    messages=[{"role": "user", "content": f"{prompt}\n\n【データ】:\n{pdf_text}"}], 
                    response_format={ "type": "json_object" }
                )
            
            clean_text = response.choices[0].message.content.strip().strip("` \t\r\n")
            if clean_text.lower().startswith("json"): clean_text = clean_text[4:].strip()
                
            raw_json = json.loads(clean_text)
            extracted_items = raw_json.get("data", [])
            if isinstance(extracted_items, dict): extracted_items = [extracted_items]

            for item in extracted_items:
                row_data = {}
                for col in desired_columns:
                    row_data[col] = item.get(col, "")
                final_orders.append(row_data)

        except Exception as e:
            return pd.DataFrame([{"システムメッセージ": f"AI解析エラー: {e}"}]), None, None

    # 📊 Excel / CSV の場合
    else:
        try:
            if file_ext == '.csv':
                try: df_file = pd.read_csv(io.BytesIO(file_bytes), encoding='utf-8')
                except: df_file = pd.read_csv(io.BytesIO(file_bytes), encoding='cp932')
            else:
                df_file = pd.read_excel(io.BytesIO(file_bytes))

            best_row_idx = -1
            max_matches = 0
            keywords = ['作品', '商品', '品名', '数', '量', '部数', '寸法', '用紙', '斤量', '色', '受注', '順', '加工', '得意先', '納期']
            for idx, row in df_file.iterrows():
                matches = sum(1 for k in keywords if any(k in str(val) for val in row.values))
                if matches > max_matches:
                    max_matches = matches
                    best_row_idx = idx

            if best_row_idx != -1 and max_matches > 0:
                df_file.columns = df_file.iloc[best_row_idx].fillna("").astype(str).tolist()
                df_file = df_file.iloc[best_row_idx+1:].reset_index(drop=True)

            mapping_dict = {}
            for src_col in df_file.columns:
                src_str = str(src_col)
                if '作品' in src_str or '商品' in src_str or '品名' in src_str: mapping_dict['作品名'] = src_col
                elif '部数' in src_str or '数' in src_str or '量' in src_str: mapping_dict['部数'] = src_col
                elif '備' in src_str or 'メモ' in src_str: mapping_dict['備考'] = src_col
                elif '得意先' in src_str or '会社' in src_str: mapping_dict['得意先'] = src_col
                elif '納期' in src_str or '納品' in src_str: mapping_dict['納期'] = src_col
                
            for target_col in desired_columns:
                if target_col not in mapping_dict:
                    for src_col in df_file.columns:
                        if target_col in str(src_col) or str(src_col) in target_col:
                            mapping_dict[target_col] = src_col
                            break

            for _, row in df_file.iterrows():
                if row.isna().all(): continue
                row_data = {}
                has_data = False
                for target_col in desired_columns:
                    source_col = mapping_dict.get(target_col)
                    if source_col and source_col in df_file.columns:
                        val = row[source_col]
                        val = val if pd.notna(val) else ""
                    else:
                        val = ""
                    row_data[target_col] = val
                    if val != "": has_data = True
                if has_data: final_orders.append(row_data)

        except Exception as e:
            return pd.DataFrame([{"システムメッセージ": f"ファイル処理エラー: {e}"}]), None, None

    if not final_orders:
        return pd.DataFrame([{"システムメッセージ": "データを抽出できませんでした。"}]), None, None

    df_result = pd.DataFrame(final_orders)
    p1, p2 = safe_create_csvs(df_result)
    return df_result, p1, p2

# =========================================================================
# 🧱 4. 画面構成
# =========================================================================
with gr.Blocks() as demo:
    gr.Markdown("## 🚀 PrintConnect (受発注データ統合システム - 本番版)")
    
    with gr.Row():
        with gr.Column(scale=1):
            gr.Markdown("### ⚙️ データ入力")
            file_input = gr.File(label="📄 発注書をドロップ (画像 / PDF / Excel)")
            custom_cols_input = gr.Textbox(
                label="🎛️ 抽出フォーマット", 
                value="順, 受注№, 作品名, 種類, 下版, 裏表, 寸法, 用紙, 斤量, 通し, 色, 部数, 備考, 加工, 加工日, 検品日, 納期, 得意先, 付合情報",
                lines=4
            )
            submit_button = gr.Button("解析して変換する", variant="primary")
            
        with gr.Column(scale=2):
            gr.Markdown("### 📊 プレビュー & ダウンロード")
            output_table = gr.Dataframe(interactive=False)
            
            with gr.Row():
                download_excel = gr.File(label="🟢 パソコン閲覧用 (UTF-8)")
                download_system = gr.File(label="🔵 システム取込用 (Shift-JIS)")
            
    submit_button.click(
        fn=process_webhook_app, 
        inputs=[file_input, custom_cols_input], 
        outputs=[output_table, download_excel, download_system]
    )

# ⚠️ 自爆する demo.launch() は完全に廃止！
# 代わりに、Gradioをプロ仕様のFastAPIサーバーに直接埋め込みます
from fastapi import FastAPI
init_app = FastAPI()
app = gr.mount_gradio_app(init_app, demo, path="/")
