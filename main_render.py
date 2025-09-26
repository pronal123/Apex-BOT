# ====================================================================================
# Apex BOT v6.0 - Render 無料Webサービス対応版 (main_render.py)
# ====================================================================================
#
# 目的: Renderの無料枠でWebサービスとして稼働させ、バックグラウンドで分析を継続する。
#
# ====================================================================================

# 1. 必要なライブラリをインポート
import os
import time
import logging
import requests
import ccxt.async_support as ccxt_async
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
import yfinance as yf
import asyncio
import random
import re 

# サーバーフレームワークのインポート
from fastapi import FastAPI
import uvicorn

# ====================================================================================
#                                    CONFIG
# ====================================================================================

JST = timezone(timedelta(hours=9))
DEFAULT_SYMBOLS = ["BTC", "ETH", "SOL", "BNB", "XRP", "LTC", "ADA", "DOGE", "AVAX", "DOT", "MATIC", "LINK", "UNI", "BCH", "FIL", "TRX", "XLM", "ICP", "ETC", "AAVE", "MKR", "ATOM", "EOS", "ALGO", "ZEC", "COMP", "NEO", "VET", "DASH", "QTUM"] 

# Render 環境変数から設定を読み込む
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '7904380124:AAE2AuRITmgBw5OECTELF5151D3pRz4K9JM')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '5890119671')
COINGLASS_API_KEY = os.environ.get('COINGLASS_API_KEY', '1d6a02becd6146a2b09ea5e424b41b6e')

# --- 動作設定 ---
LOOP_INTERVAL = 30       # メイン分析ループの実行間隔を30秒に設定
DYNAMIC_UPDATE_INTERVAL = 300 # 出来高ランキング更新間隔を300秒 (5分) に設定

# --- APIエンドポイント ---
COINGLASS_API_HEADERS = {'accept': 'application/json', 'coinglass-api-key': COINGLASS_API_KEY}

# ====================================================================================
#                               UTILITIES & CLIENTS
# ====================================================================================

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

CCXT_CLIENT_NAME = 'Binance Futures' 
CCXT_CLIENT = None 
LAST_UPDATE_TIME = 0.0 
CURRENT_MONITOR_SYMBOLS = []
PROXY_LIST = []

def initialize_ccxt_client(proxy_options: Dict = {}):
    """プロキシオプションを適用してBinanceクライアントを初期化/再初期化する"""
    global CCXT_CLIENT
    
    if CCXT_CLIENT:
        # クライアントの再初期化前に必ず閉じる（Render環境ではこれを確実に行う）
        try:
             asyncio.create_task(CCXT_CLIENT.close())
        except Exception:
             pass

    base_options = {"enableRateLimit": True, "timeout": 15000}
    CCXT_CLIENT = ccxt_async.binance({**base_options, **proxy_options, "options": {"defaultType": "future"}})

# (get_proxy_list_from_web, get_proxy_options, send_telegram_html, get_tradfi_macro_context, calculate_kama の各関数は省略)
# 👆 簡潔性のため、これらの関数（前回答の最終コードにあるもの）は全てmain_render.pyにコピー＆ペーストしてください。

# **【重要】以下の関数は前回の最終コード（プロキシ強化版）からそのままコピーしてください**
# async def fetch_top_symbols_binance_or_default(limit: int = 30) -> Tuple[List[str], str]: 
# async def fetch_ohlcv_async(symbol: str, timeframe: str, limit: int) -> List[list]:
# async def fetch_market_sentiment_data_async(symbol: str) -> Dict:
# def get_tradfi_macro_context() -> str:
# def calculate_kama(prices: pd.Series, period: int = 10, fast_ema: int = 2, slow_ema: int = 30) -> pd.Series:
# async def determine_market_regime(symbol: str) -> str:
# async def multi_timeframe_confirmation(symbol: str) -> (bool, str):
# def get_ml_prediction(ohlcv: List[list], sentiment: Dict) -> float:
# async def generate_signal(symbol: str, regime: str, macro_context: str) -> Optional[Dict]:
# def format_telegram_message(signal: Dict) -> str:
# async def analyze_symbol_and_notify(symbol: str, macro_context: str, notified_symbols: Dict):

# ------------------------------------------------------------------------------------
# 【Render対応】メインループの構造変更
# ------------------------------------------------------------------------------------

async def main_loop():
    """BOTの常時監視を実行するバックグラウンドタスク"""
    global LAST_UPDATE_TIME, CURRENT_MONITOR_SYMBOLS
    notified_symbols = {}
    
    # 起動時の初期リスト設定
    CURRENT_MONITOR_SYMBOLS = DEFAULT_SYMBOLS[:30]
    
    # 最初のマクロコンテクスト取得
    macro_context = get_tradfi_macro_context() 
    
    while True:
        try:
            current_time = time.time()
            is_dynamic_update_needed = (current_time - LAST_UPDATE_TIME) >= DYNAMIC_UPDATE_INTERVAL
            
            # --- 動的更新フェーズ (300秒に一度) ---
            if is_dynamic_update_needed:
                logging.info("--- DYNAMIC UPDATE START ---")
                macro_context = get_tradfi_macro_context() # マクロコンテクストを更新
                logging.info(f"マクロ経済コンテクスト: {macro_context}")
                
                # 出来高TOP30取得試行 (プロキシリトライロジックを使用)
                symbols_to_monitor, source_exchange = await fetch_top_symbols_binance_or_default(30)
                
                CURRENT_MONITOR_SYMBOLS = symbols_to_monitor
                LAST_UPDATE_TIME = current_time
                
                logging.info(f"銘柄選定元: {source_exchange}. 監視対象: {CURRENT_MONITOR_SYMBOLS[:3]}...")
            
            # --- メイン分析実行 (30秒ごと) ---
            
            # 分析の実行
            tasks = [analyze_symbol_and_notify(sym, macro_context, notified_symbols) for sym in CURRENT_MONITOR_SYMBOLS]
            await asyncio.gather(*tasks)

            # 次のサイクルまでの待機
            await asyncio.sleep(LOOP_INTERVAL)
            
        except asyncio.CancelledError:
            logging.warning("バックグラウンドタスクがキャンセルされました。")
            break
        except Exception as e:
            logging.error(f"メインループで予期せぬエラーが発生しました: {e}。{LOOP_INTERVAL}秒後に再試行します。")
            await asyncio.sleep(LOOP_INTERVAL)


# ------------------------------------------------------------------------------------
# FASTAPI WEB SERVER SETUP
# ------------------------------------------------------------------------------------

app = FastAPI()

@app.on_event("startup")
async def startup_event():
    """サーバー起動時にクライアントを初期化し、バックグラウンドタスクを開始する"""
    global PROXY_LIST
    
    logging.info("Starting Apex BOT Web Service...")
    
    # 1. プロキシリストの初期化 (同期実行)
    PROXY_LIST = get_proxy_list_from_web()

    # 2. CCXTクライアントの初期化 (最初の試行はプロキシなし)
    initialize_ccxt_client() 

    # 3. バックグラウンドタスクとしてメインループを起動
    asyncio.create_task(main_loop())
    
@app.on_event("shutdown")
async def shutdown_event():
    """サーバーシャットダウン時にリソースを解放する"""
    if CCXT_CLIENT:
        logging.info("Closing CCXT Client during shutdown.")
        await CCXT_CLIENT.close()

@app.get("/")
def read_root():
    """Renderのスリープを防ぐためのヘルスチェックエンドポイント"""
    return {
        "status": "Running",
        "service": "Apex BOT v6.0",
        "next_update": f"{DYNAMIC_UPDATE_INTERVAL - (time.time() - LAST_UPDATE_TIME):.0f}s"
    }

# ------------------------------------------------------------------------------------
# RENDER ENTRY POINT
# ------------------------------------------------------------------------------------

# if __name__ == "__main__":
#     # Renderは gunicorn/uvicorn で起動されるため、以下のブロックは通常コメントアウト
#     uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
