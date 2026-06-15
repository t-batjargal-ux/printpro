import gradio as gr
import gradio.networking
import os
import json
import base64
import io
import pandas as pd
from pypdf import PdfReader
import tempfile
from openai import OpenAI

# =========================================================================
# 🚨 1. Render特有の「localhost起動エラー」をスキップする魔法
# =========================================================================
gradio.networking.url_ok = lambda *args, **kwargs: True

# =========================================================================
# 🚨 2. 500エラー（bool is not iterable）を防ぐ特効薬
# =========================================================================
import gradio_client.utils
orig_json_schema_to_python_type = gradio_client.utils._json_schema_to_python_type
def patched_json_schema_to_python_type(schema, defs=None):
    if isinstance(schema, bool):
        return "any"
    return orig_json_schema_to_python_type(schema, defs)
gradio_client.utils._json_schema_to_python_type = patched_json_schema_to_python_type

# =========================================================================
# 🔒 システム起動 ＆ ログイン情報取得
# =========================================================================
print("🔒 システム起動中... (AIフル稼働・表記ゆれ対応版)")
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')

LOGIN_USER = os.environ.get('LOGIN_USER', 'admin')
LOGIN_PASS = os.environ.get('LOGIN_PASS', 'print2026')

def safe_create_csvs(df):
    tmp_dir = tempfile.gettempdir()
    p1 = os.path.join(tmp_dir, "1_パソコン閲覧用_UTF8.csv")
    p2 = os.path.join(tmp_dir, "2_システム取込用_ShiftJIS.csv")
    df.to_csv(p1, index=False, encoding='utf-8-sig')
    df.to_csv(p2, index=False, encoding='cp932', errors='replace')
    return p1, p2

def process_webhook_app(uploaded_files, custom_cols_str):
    if not uploaded_files: 
        return pd.DataFrame([{"システムメッセージ": "ファイルが選択されていません。"}]), None, None
    
    if not OPENAI_API_KEY:
        return pd.DataFrame([{"システムメッセージ": "Renderの環境変数（OPENAI_API_KEY）が設定されていません。"}], columns=["システムメッセージ"]), None, None
        
    desired_columns = [c.strip() for c in custom_cols_str.split(',') if c.strip()]
    client = OpenAI(api_key=OPENAI_API_KEY.strip())
    final_orders = []

    if not isinstance(uploaded_files, list):
        uploaded_files = [uploaded_files]

    # 💡 AIへの強力な指示書（科目名が違っても空気を読んでマッピングさせる）
    prompt = f"""
    あなたは正確無比なデータ入力のプロフェッショナルです。
    提供される発注書データ（画像またはテキスト）を解析し、必要な項目を抽出して指定されたJSONフォーマットで出力してください。

    【超重要指示：柔軟な項目マッピング】
    - 企業や書類ごとに「項目名（科目名）」が異なっていても、文脈を深く理解して指定されたキーに柔軟にマッピングしてください。
      （例：「品名」「商品名」「タイトル」→「作品名」へ）
      （例：「数量」「注文数」「ロット」→「部数」へ）
      （例：「得意先」「クライアント」「発注元」→「得意先」へ）
    - 1つのファイルに複数の注文（明細行）がある場合は、配列 `data` の中に複数のオブジェクトを作成してください。
    - どうしても抽出できない項目、または記載がない項目は必ず空文字 "" にしてください。

    【出力JSONフォーマット】
    {{ "data": [ {{ "キー1": "値1", "キー2": "値2" }} ] }}

    【必須の抽出項目キー（このキー名を厳守してください）】: {desired_columns}
    """

    for uploaded_file in uploaded_files:
        try:
            file_path = uploaded_file if isinstance(uploaded_file, str) else uploaded_file.name
            file_name = os.path.basename(file_path)
            file_ext = os.path.splitext(file_path)[1].lower()
            with open(file_path, "rb") as f: file_bytes = f.read()
        except Exception as e:
            final_orders.append({"元ファイル名": "読込エラー", "システムメッセージ": f"読込失敗: {e}"})
            continue

        try:
            # 📸 画像の場合
            if file_ext in ['.jpg', '.jpeg', '.png']:
                base64_img = base64.b64encode(file_bytes).decode('utf-8')
                response = client.chat.completions.create(
                    model="gpt-4o",
                    messages=[{"role": "user", "content": [{"type": "text", "text": prompt},{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_img}"}}]}],
                    response_format={ "type": "json_object" }
                )
            
            # 📄 PDF・Excel・CSV の場合（すべてテキスト化してAIに読ませる）
            else:
                if file_ext == '.pdf':
                    extracted_text = "".join([page.extract_text() + "\n" for page in PdfReader(io.BytesIO(file_bytes)).pages])
                elif file_ext == '.csv':
                    try: df_file = pd.read_csv(io.BytesIO(file_bytes), encoding='utf-8')
                    except: df_file = pd.read_csv(io.BytesIO(file_bytes), encoding='cp932')
                    # 長すぎるファイルのエラーを防ぐため、先頭300行だけをテキスト化
                    extracted_text = df_file.head(300).to_csv(index=False)
                else:
                    df_file = pd.read_excel(io.BytesIO(file_bytes))
                    extracted_text = df_file.head(300).to_csv(index=False)

                response = client.chat.completions.create(
                    model="gpt-4o", 
                    messages=[{"role": "user", "content": f"{prompt}\n\n【提供データ】:\n{extracted_text}"}], 
                    response_format={ "type": "json_object" }
                )
            
            # AIからのJSONデータをきれいに取り出す
            clean_text = response.choices[0].message.content.strip().strip("` \t\r\n")
            if clean_text.lower().startswith("json"): clean_text = clean_text[4:].strip()
                
            raw_json = json.loads(clean_text)
            extracted_items = raw_json.get("data", [])
            if isinstance(extracted_items, dict): extracted_items = [extracted_items]

            for item in extracted_items:
                row_data = {"元ファイル名": file_name}
                for col in desired_columns:
                    row_data[col] = item.get(col, "")
                final_orders.append(row_data)

        except Exception as e:
            final_orders.append({"元ファイル名": file_name, "システムメッセージ": f"AI解析エラー: {e}"})

    if not final_orders:
        return pd.DataFrame([{"システムメッセージ": "データを抽出できませんでした。"}]), None, None

    df_result = pd.DataFrame(final_orders)
    
    if "システムメッセージ" in df_result.columns and df_result["システムメッセージ"].isna().all():
        df_result.drop(columns=["システムメッセージ"], inplace=True)
        
    p1, p2 = safe_create_csvs(df_result)
    return df_result, p1, p2

# =========================================================================
# 🧱 画面構成
# =========================================================================
with gr.Blocks() as demo:
    gr.Markdown("## 🚀 PrintConnect (受発注データ統合システム - 本番版)")
    
    with gr.Row():
        with gr.Column(scale=1):
            gr.Markdown("### ⚙️ データ入力")
            file_input = gr.File(label="📄 発注書をドロップ (複数選択可)", file_count="multiple")
            custom_cols_input = gr.Textbox(
                label="🎛️ 抽出フォーマット", 
                value="順, 受注№, 作品名, 種類, 下版, 裏表, 寸法, 用紙, 斤量, 通し, 色, 部数, 備考, 加工, 加工日, 検品日, 納期, 得意先, 付合情報",
                lines=4
            )
            submit_button = gr.Button("一括解析して変換する", variant="primary")
            
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

demo.launch(server_name="0.0.0.0", server_port=10000, auth=(LOGIN_USER, LOGIN_PASS))
