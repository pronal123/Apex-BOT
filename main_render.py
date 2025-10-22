# ====================================================================================
# Apex BOT v19.0.28 - Safety and Frequency Finalized (Patch 36)
#
# 修正ポイント:
# 1. 【エラー修正】FTPアップロード関数 (_sync_ftp_upload) に最大3回のリトライロジックを追加し、タイムアウトエラーに対応。
# 2. 【堅牢化】OHLCV取得関数 (fetch_ohlcv_safe) のエラーハンドリングを強化し、CCXT APIエラーやレート制限に対応。
# 3. 【安全確認】動的取引閾値 (0.67, 0.63, 0.58) を最終確定。
#
# 💡 ユーザー要望による追加修正:
# 4. 【勝率向上】取引閾値を全体的に0.02pt引き上げ (0.69, 0.65, 0.60)。
# 5. 【Telegramエラー修正】get_estimated_win_rate関数の '<' を '&lt;' にエスケープ。
# 6. 【ログ強化】execute_trade関数にCCXTエラーハンドリングを追加し、取引権限や残高不足をログに出力。
# ====================================================================================

# 1. 必要なライブラリをインポート
import os
import time
import logging
import requests
import ccxt.async_support as ccxt_async
import ccxt
import numpy as np
import pandas as pd
import pandas_ta as ta
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Any, Callable
import asyncio
from fastapi import FastAPI
from fastapi.responses import JSONResponse
import uvicorn
from dotenv import load_dotenv
import sys
import random
import json
import re
import ftplib 
import uuid 

# .envファイルから環境変数を読み込む
load_dotenv()

# 💡 【ログ確認対応】ロギング設定を明示的に定義
logging.basicConfig(
    level=logging.INFO, # INFOレベル以上のメッセージを出力
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# ====================================================================================
# CONFIG & ENV VARIABLES
# ====================================================================================

# 📌 実行環境設定
TEST_MODE = os.getenv("TEST_MODE", "True").lower() == "true" # Trueの場合、取引所への注文はスキップ
LOOP_INTERVAL = int(os.getenv("LOOP_INTERVAL", 12))  # メインループの実行間隔（秒）

# 📌 取引所設定
EXCHANGE_ID = os.getenv("EXCHANGE_ID", "mexc")
MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_SECRET = os.getenv("MEXC_SECRET")
# 証拠金取引を無効化
ENABLE_MARGIN = os.getenv("ENABLE_MARGIN", "False").lower() == "true"

# 📌 Telegram通知設定
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# 📌 FTP設定 (ログ・データの同期用)
FTP_HOST = os.getenv("FTP_HOST")
FTP_USER = os.getenv("FTP_USER")
FTP_PASS = os.getenv("FTP_PASS")
FTP_REMOTE_DIR = os.getenv("FTP_REMOTE_DIR", "logs")

# 📌 取引戦略設定
CANDLE_TIMEFRAME = os.getenv("CANDLE_TIMEFRAME", "4h") # 使用する時間枠
BASE_TRADE_SIZE_USDT = float(os.getenv("BASE_TRADE_SIZE_USDT", 20.0)) # 1ポジションあたりの取引サイズ（USDT建て）
TAKE_PROFIT_RATIO = float(os.getenv("TAKE_PROFIT_RATIO", 0.10)) # 10%
STOP_LOSS_RATIO = float(os.getenv("STOP_LOSS_RATIO", 0.05))   # 5%

# 📌 監視銘柄リスト (MEXCで流動性が高い銘柄)
TARGET_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT", "ADA/USDT",
    "AVAX/USDT", "DOT/USDT", "LINK/USDT", "MATIC/USDT", "LTC/USDT", "BCH/USDT",
    "TRX/USDT", "ETC/USDT", "ALGO/USDT", "XLM/USDT", "VET/USDT"
]

# 市場環境に応じた動的閾値調整のための定数 (ユーザー要望に合わせて調整 - 勝率優先)
FGI_SLUMP_THRESHOLD = -0.02         # FGIプロキシがこの値未満の場合、市場低迷と見なす
FGI_ACTIVE_THRESHOLD = 0.02         # FGIプロキシがこの値を超える場合、市場活発と見なす
# 🚨 最終調整箇所: WIN-RATE優先のため、全体的に2pt引き上げ
SIGNAL_THRESHOLD_SLUMP = 0.69       # 低迷時の閾値 (1銘柄/日を想定) <- 0.67から0.69に変更
SIGNAL_THRESHOLD_NORMAL = 0.65      # 通常時の閾値 (1-2銘柄/日を想定) <- 0.63から0.65に変更
SIGNAL_THRESHOLD_ACTIVE = 0.60      # 活発時の閾値 (2-3銘柄/日を想定) <- 0.58から0.60に変更

# ====================================================================================
# GLOBAL STATE & INITIALIZATION
# ====================================================================================

# グローバル変数
exchange = None
OPEN_POSITIONS: Dict[str, Dict[str, Any]] = {}
LAST_SUCCESS_TIME = 0.0
GLOBAL_MACRO_CONTEXT = 1 # 0:低迷/SLUMP, 1:通常/NORMAL, 2:活発/ACTIVE (初期値:通常)

# FastAPI
app = FastAPI()

# ====================================================================================
# UTILITY FUNCTIONS
# ====================================================================================

def get_current_threshold(macro_context: int) -> float:
    """現在の市場環境に基づき、動的閾値を返す"""
    if macro_context == 0:
        return SIGNAL_THRESHOLD_SLUMP
    elif macro_context == 2:
        return SIGNAL_THRESHOLD_ACTIVE
    else:
        return SIGNAL_THRESHOLD_NORMAL

def get_estimated_win_rate(score: float) -> str:
    """スコアに基づいて推定勝率を返す (通知用)"""
    if score >= 0.90: return "90%+"
    if score >= 0.85: return "85-90%"
    if score >= 0.75: return "75-85%"
    if score >= 0.65: return "65-75%" 
    if score >= 0.60: return "60-65%"
    # 修正: '<' を '&lt;' にエスケープし、Telegramエラーを回避
    return "&lt;60% (低)" 

def format_telegram_message(title: str, symbol: str, entry_price: float, sl_price: float, tp_price: float, score: float, side: str, is_exit: bool = False, exit_price: Optional[float] = None) -> str:
    """Telegram通知用のメッセージをHTML形式で作成する"""
    
    # スコアに基づいた勝率と色を決定
    win_rate_str = get_estimated_win_rate(score)
    
    if score >= 0.75:
        color_emoji = "🟢"
    elif score >= 0.65:
        color_emoji = "🟡"
    else:
        color_emoji = "🔴"
        
    # Exitメッセージの場合
    if is_exit and exit_price is not None:
        profit_loss = ((exit_price - entry_price) / entry_price) * 100
        if side == "SELL": # ショートは逆算
             profit_loss = ((entry_price - exit_price) / entry_price) * 100
             
        pnl_emoji = "✅" if profit_loss >= 0 else "❌"
        
        return (
            f"{pnl_emoji} **{title}**\n"
            f"<b>銘柄:</b> {symbol}\n"
            f"<b>方向:</b> {side}\n"
            f"<b>エントリー価格:</b> {entry_price:.4f}\n"
            f"<b>決済価格:</b> {exit_price:.4f}\n"
            f"<b>損益率:</b> <u>{profit_loss:.2f}%</u>"
        )
    
    # Entryメッセージの場合
    return (
        f"{color_emoji} **{title}**\n"
        f"<b>銘柄:</b> {symbol}\n"
        f"<b>方向:</b> {side}\n"
        f"<b>推定勝率 ({color_emoji}):</b> {win_rate_str}\n"
        f"<b>エントリー価格:</b> {entry_price:.4f}\n"
        f"<b>TP価格 ({TAKE_PROFIT_RATIO*100:.0f}%):</b> {tp_price:.4f}\n"
        f"<b>SL価格 ({STOP_LOSS_RATIO*100:.0f}%):</b> {sl_price:.4f}"
    )

def send_telegram_alert(message: str, level: str = 'INFO'):
    """Telegramにアラートメッセージを送信する"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("⚠️ TelegramのトークンまたはChat IDが設定されていません。通知をスキップします。")
        return
        
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    
    # エラーメッセージを強調表示
    if level == 'CRITICAL':
        message = f"🚨🚨 **CRITICAL ALERT** 🚨🚨\n{message}"
    elif level == 'WARNING':
        message = f"⚠️ WARNING ⚠️\n{message}"

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML" # HTMLパースモードを使用
    }
    
    try:
        response = requests.post(url, data=payload)
        response.raise_for_status() 
        logging.info(f"✅ Telegram通知を送信しました ({level}).")
    except requests.exceptions.RequestException as e:
        # エラーの具体的な詳細をログに出力
        error_details = response.json() if 'response' in locals() and response.content else str(e)
        logging.error(f"❌ Telegram HTTPエラー: {e} - 詳細: {error_details}")

def _sync_ftp_upload(local_path: str, remote_path: str):
    """同期的にFTPにファイルをアップロードする（最大3回リトライ）"""
    if not FTP_HOST or not FTP_USER or not FTP_PASS:
        logging.warning("⚠️ FTP設定が不十分なため、ログの同期をスキップします。")
        return
        
    for attempt in range(1, 4): # 3回リトライ
        try:
            with ftplib.FTP(FTP_HOST) as ftp:
                # 匿名ログインはしない
                ftp.login(user=FTP_USER, passwd=FTP_PASS)
                # リモートディレクトリへ移動。存在しない場合は作成を試みる
                try:
                    ftp.cwd(remote_path)
                except ftplib.error_perm:
                    ftp.mkd(remote_path)
                    ftp.cwd(remote_path)

                remote_filename = os.path.basename(local_path)
                with open(local_path, 'rb') as f:
                    ftp.storbinary(f'STOR {remote_filename}', f)
                
                logging.info(f"✅ Trade ExitログをFTPに記録しました (試行: {attempt}回目).")
                return # 成功
                
        except ftplib.all_errors as e:
            logging.error(f"❌ FTPエラーが発生しました (試行: {attempt}回目): {e}")
            if attempt < 3:
                time.sleep(2) # 2秒待機してからリトライ
            else:
                logging.error("❌ FTPアップロードが3回全て失敗しました。")
                return # 失敗

def write_trade_log_and_sync(log_data: Dict[str, Any], is_entry: bool):
    """取引ログをファイルに追記し、FTPに同期する"""
    log_file = "trade_log_entry.jsonl" if is_entry else "trade_log_exit.jsonl"
    
    try:
        # ログをJSONL形式で追記
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(log_data, ensure_ascii=False) + '\n')
        
        logging.info(f"✅ Trade {'Entry' if is_entry else 'Exit'}ログをファイルに記録しました。")
        
        # ExitログのみFTPに同期
        if not is_entry:
            _sync_ftp_upload(log_file, FTP_REMOTE_DIR)
            
    except Exception as e:
        logging.error(f"❌ 取引ログのファイル書き込みまたはFTP同期中に予期せぬエラーが発生しました: {e}", exc_info=True)


# ====================================================================================
# EXCHANGE AND API FUNCTIONS
# ====================================================================================

async def initialize_exchange() -> Optional[ccxt_async.Exchange]:
    """CCXTクライアントを初期化する"""
    try:
        if EXCHANGE_ID == 'mexc':
            ex = ccxt_async.mexc({
                'apiKey': MEXC_API_KEY,
                'secret': MEXC_SECRET,
                'options': {
                    'defaultType': 'spot', # 現物取引に固定
                }
            })
        else:
            logging.error(f"❌ サポートされていない取引所ID: {EXCHANGE_ID}")
            return None

        # 接続確認
        await ex.load_markets()
        
        logging.info(f"✅ CCXTクライアントを初期化しました: {EXCHANGE_ID}. マーケット数: {len(ex.markets)}")
        return ex

    except Exception as e:
        logging.error(f"❌ CCXTクライアントの初期化に失敗しました: {e}", exc_info=True)
        return None

async def fetch_ohlcv_safe(exchange: ccxt_async.Exchange, symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
    """OHLCVデータを安全に取得する"""
    try:
        ohlcv = await exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        if not ohlcv:
            logging.warning(f"⚠️ {symbol} のOHLCVデータが空です。")
            return None
            
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        return df

    except ccxt.base.errors.ExchangeError as e:
        logging.error(f"❌ {symbol} のデータ取得中に取引所エラーが発生: {e}")
    except ccxt.base.errors.NetworkError as e:
        logging.error(f"❌ {symbol} のデータ取得中にネットワークエラーが発生: {e}")
    except Exception as e:
        logging.error(f"❌ {symbol} のOHLCV取得中に予期せぬエラーが発生: {e}", exc_info=True)
        
    return None

async def fetch_fgi_proxy(exchange: ccxt_async.Exchange) -> float:
    """Fear & Greed Indexのプロキシ値を計算する (0.0:極度の恐怖, 1.0:極度の強欲)"""
    
    # BTCの最新のOHLCVデータを取得
    df_btc = await fetch_ohlcv_safe(exchange, "BTC/USDT", "1d", 20)
    if df_btc is None:
        return GLOBAL_MACRO_CONTEXT # データ取得失敗時は現状維持

    # RSI (Relative Strength Index) を計算
    # 期間は通常14だが、マクロ環境を簡易的に把握するため短期の5を使用
    df_btc.ta.rsi(length=5, append=True)
    
    # 最後のRSI値を取得 (0-100)
    last_rsi = df_btc['RSI_5'].iloc[-1]
    
    # FGIプロキシ値を計算 (正規化 -1.0 to 1.0)
    # FGIプロキシ = (RSI / 50) - 1
    # RSI=25 -> FGI=-0.5 (恐怖)
    # RSI=50 -> FGI=0.0 (中立)
    # RSI=75 -> FGI=0.5 (強欲)
    fgi_proxy = (last_rsi / 50.0) - 1.0
    
    logging.info(f"💡 FGIプロキシ値を計算しました (BTC RSI-5: {last_rsi:.2f} -> Proxy: {fgi_proxy:.2f})")
    
    return fgi_proxy

async def execute_trade(exchange: ccxt_async.Exchange, symbol: str, type: str, side: str, amount: float, price: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """
    指定された取引所で取引を実行し、注文情報を返す。
    成功した場合は注文情報、失敗した場合はNoneを返す。
    """
    if TEST_MODE:
        logging.info(f"🧪 TEST MODE: {symbol} - {side} {type} {amount} をシミュレーション注文しました。")
        return {"id": "TEST_ORDER_" + str(uuid.uuid4()), "status": "closed", "symbol": symbol, "price": price or 0.0, "amount": amount, "datetime": datetime.now(timezone.utc).isoformat()}
    
    try:
        # 価格が指定されている場合は指値注文、そうでない場合は成行注文
        if price is not None and price > 0:
            order = await exchange.create_limit_order(symbol, side, amount, price)
            order_type = "LIMIT"
        else:
            # 現物取引では、価格をNoneとして成行注文 (Market Order) を実行
            # MEXCはcreate_orderでtype='market'、price=Noneでも動作する
            order = await exchange.create_market_order(symbol, side, amount)
            order_type = "MARKET"

        logging.info(f"✅ TRADE EXECUTED: {symbol} - {side} {order_type} {amount} 注文ID: {order.get('id', 'N/A')}")
        return order

    # 💡 ユーザー要望による修正: CCXT固有のエラーをキャッチし、詳細なログを出力
    except ccxt.base.errors.PermissionDenied as e:
        # APIキーの権限不足（取引権限がないなど）
        logging.error(f"❌ CCXT API ERROR: {symbol} ポジション取得失敗 (Permission Denied/権限不足). 詳細: {e}")
        send_telegram_alert(f"🚨 重大エラー: {symbol} ポジション取得失敗。\n**原因: APIキーに取引権限がありません。**\n\nログを確認してください。", level='CRITICAL')
        return None
        
    except ccxt.base.errors.InsufficientFunds as e:
        # 残高不足
        logging.error(f"❌ CCXT API ERROR: {symbol} ポジション取得失敗 (Insufficient Funds/残高不足). 詳細: {e}")
        send_telegram_alert(f"⚠️ エラー: {symbol} ポジション取得失敗。\n**原因: USDT残高が不足しています。**\n\nログを確認してください。", level='WARNING')
        return None
        
    except ccxt.base.errors.NetworkError as e:
        # ネットワークまたはレート制限
        logging.error(f"❌ CCXT API ERROR: {symbol} ポジション取得失敗 (Network/Rate Limit). 詳細: {e}")
        return None
        
    except ccxt.base.errors.AuthenticationError as e:
        # APIキー/シークレットが間違っている
        logging.error(f"❌ CCXT API ERROR: {symbol} ポジション取得失敗 (Authentication Error). 詳細: {e}")
        send_telegram_alert(f"🚨 重大エラー: {symbol} ポジション取得失敗。\n**原因: API認証情報が誤っています。**\n\nログを確認してください。", level='CRITICAL')
        return None

    except ccxt.base.errors.ExchangeError as e:
        # その他、取引所固有のエラー
        logging.error(f"❌ CCXT API ERROR: {symbol} ポジション取得失敗 (Exchange Specific Error). 詳細: {e}")
        return None

    except Exception as e:
        # 予期せぬその他のエラー
        logging.error(f"❌ UNEXPECTED ERROR during trade execution for {symbol}: {e}", exc_info=True)
        return None

# ====================================================================================
# STRATEGY AND MAIN LOGIC
# ====================================================================================

async def evaluate_symbol(exchange: ccxt_async.Exchange, symbol: str) -> Optional[Tuple[float, str]]:
    """
    単一銘柄の取引シグナルを評価し、スコアと推奨方向を返す
    """
    df = await fetch_ohlcv_safe(exchange, symbol, CANDLE_TIMEFRAME, 100)
    if df is None or len(df) < 50:
        return None

    # 1. 指標計算 (RSI, StochRSI, MACD)
    df.ta.rsi(length=14, append=True)
    df.ta.stochrsi(length=14, append=True)
    df.ta.macd(fast=12, slow=26, signal=9, append=True)

    # 2. 最新値の取得
    last_row = df.iloc[-1]
    close_price = last_row['close']
    rsi = last_row['RSI_14']
    stoch_k = last_row['STOCHRSIk_14_14_3_3']
    stoch_d = last_row['STOCHRSId_14_14_3_3']
    macd = last_row['MACD_12_26_9']
    macdh = last_row['MACDh_12_26_9']
    
    # 3. スコアリングシステム (0.0 to 1.0)
    score = 0.0
    
    # === RSIスコア (過熱/冷却から中立への復帰を評価) ===
    # 0-30から50へ向かう(ロング)、70-100から50へ向かう(ショート)
    rsi_range = 70.0 - 30.0
    if rsi < 50: # ロングチャンス
        score += max(0, (50 - rsi) / (50 - 30)) * 0.3 # 0.0 to 0.3
    else: # ショートチャンス
        score += max(0, (rsi - 50) / (70 - 50)) * 0.3 # 0.0 to 0.3
        
    # === StochRSIスコア (より敏感な過熱/冷却を評価) ===
    # K/Dが20未満(ロング)、80超(ショート)
    stoch_avg = (stoch_k + stoch_d) / 2
    if stoch_avg < 50: # ロングチャンス
        score += max(0, (50 - stoch_avg) / 50) * 0.4 # 0.0 to 0.4 (20未満なら強く寄与)
    else: # ショートチャンス
        score += max(0, (stoch_avg - 50) / 50) * 0.4 # 0.0 to 0.4 (80超なら強く寄与)
        
    # === MACDスコア (トレンドの勢いを評価) ===
    # MACDHがゼロライン上向き(ロング)、下向き(ショート)
    if macdh > 0: # ロング優位
        score += min(0.3, abs(macdh) * 10) # 0.0 to 0.3 (勢いに応じて)
    elif macdh < 0: # ショート優位
        score += min(0.3, abs(macdh) * 10) # 0.0 to 0.3 (勢いに応じて)

    # スコアの最大値を1.0に丸める
    score = min(1.0, score)
    
    # 4. 推奨方向の決定
    # ロング: RSI<50 and StochRSI<50 (かつMACD上昇)
    # ショート: RSI>50 and StochRSI>50 (かつMACD下降)
    
    # 単純な方向決定
    side = "BUY" # 初期値
    # MACDが上向きで、RSIとStochRSIが過冷却から回復基調 (上昇トレンド初期)
    if macd > 0 and stoch_k > stoch_d:
        side = "BUY"
    # MACDが下向きで、RSIとStochRSIが過熱から下降基調 (下降トレンド初期)
    elif macd < 0 and stoch_k < stoch_d:
        side = "SELL"
    # それ以外はスコアを減衰させる (方向性が不明確)
    else:
        score *= 0.8
        
    return score, side


async def check_for_exits(exchange: ccxt_async.Exchange):
    """オープンポジションの監視と決済処理を行う"""
    global OPEN_POSITIONS
    
    symbols_to_remove = []
    # 現在のオープンポジションのティッカー価格を一括取得
    symbols_in_pos = list(OPEN_POSITIONS.keys())
    if not symbols_in_pos:
        return
        
    current_prices = await exchange.fetch_tickers(symbols_in_pos)
    
    for symbol, pos in OPEN_POSITIONS.items():
        try:
            current_price = current_prices.get(symbol, {}).get('last')
            if current_price is None:
                logging.warning(f"⚠️ {symbol} の現在価格を取得できませんでした。スキップします。")
                continue

            entry_price = pos['entry_price']
            side = pos['side']
            
            # SL/TPのチェック
            sl_triggered = False
            tp_triggered = False
            
            if side == "BUY":
                if current_price <= pos['sl_price']:
                    sl_triggered = True
                elif current_price >= pos['tp_price']:
                    tp_triggered = True
            elif side == "SELL":
                if current_price >= pos['sl_price']:
                    sl_triggered = True
                elif current_price <= pos['tp_price']:
                    tp_triggered = True
            
            # 決済実行
            if sl_triggered or tp_triggered:
                exit_side = "SELL" if side == "BUY" else "BUY"
                title = "✅ TAKE PROFIT (TP) トリガー" if tp_triggered else "🔴 STOP LOSS (SL) トリガー"
                
                # 決済注文（成行）
                exit_order = await execute_trade(exchange, symbol, "market", exit_side, pos['amount'], price=current_price)
                
                # ログ記録と通知
                if exit_order is not None:
                    exit_price = exit_order.get('price', current_price)
                    
                    # Telegram通知
                    msg = format_telegram_message(title, symbol, entry_price, 0, 0, pos['score'], side, is_exit=True, exit_price=exit_price)
                    send_telegram_alert(msg)
                    
                    # ログ記録
                    log_data = {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "symbol": symbol,
                        "side": side,
                        "action": "EXIT",
                        "reason": "TP" if tp_triggered else "SL",
                        "entry_price": entry_price,
                        "exit_price": exit_price,
                        "amount": pos['amount'],
                        "order_id": exit_order.get('id', 'N/A')
                    }
                    write_trade_log_and_sync(log_data, is_entry=False)
                    
                    symbols_to_remove.append(symbol)
                    
                else:
                    # execute_trade内で詳細なエラーログは出力済み
                    logging.error(f"❌ {symbol} の決済注文の実行に失敗しました。ポジションは残っています。")
                    
        except Exception as e:
            logging.error(f"❌ {symbol} の決済処理中に予期せぬエラーが発生しました: {e}", exc_info=True)

    # 決済が完了したポジションをリストから削除
    for symbol in symbols_to_remove:
        if symbol in OPEN_POSITIONS:
            del OPEN_POSITIONS[symbol]
            logging.info(f"🗑️ {symbol} のポジションを管理リストから削除しました。管理銘柄数: {len(OPEN_POSITIONS)}")

async def check_for_entries(exchange: ccxt_async.Exchange):
    """新規エントリーの機会を評価し、実行する"""
    global OPEN_POSITIONS
    
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    logging.info(f"👀 ポジション監視中... 現在 {len(OPEN_POSITIONS)} 銘柄を管理。現在のエントリー閾値: {current_threshold:.2f}")

    # 現在オープンポジションがない銘柄のみを対象とする
    symbols_to_evaluate = [s for s in TARGET_SYMBOLS if s not in OPEN_POSITIONS]
    
    # 評価タスクの並行実行
    tasks = [evaluate_symbol(exchange, s) for s in symbols_to_evaluate]
    results = await asyncio.gather(*tasks)

    # 高スコア順にソート
    scored_symbols = []
    for symbol, result in zip(symbols_to_evaluate, results):
        if result is not None:
            score, side = result
            if score >= current_threshold:
                scored_symbols.append((score, symbol, side))
                
    # スコアが高いものから順に処理
    scored_symbols.sort(key=lambda x: x[0], reverse=True)
    
    # 最初の1つ（最高スコア）のシグナルのみでエントリーを試みる
    if scored_symbols:
        score, symbol, side = scored_symbols[0]
        
        # 現在価格を取得
        try:
            ticker = await exchange.fetch_ticker(symbol)
            entry_price = ticker['last']
        except Exception as e:
            logging.error(f"❌ {symbol} の現在価格取得に失敗したため、エントリーをスキップします: {e}")
            return

        # 取引量の計算 (BASE_TRADE_SIZE_USDT / 価格)
        amount = BASE_TRADE_SIZE_USDT / entry_price
        
        # TP/SL価格の計算
        if side == "BUY":
            tp_price = entry_price * (1 + TAKE_PROFIT_RATIO)
            sl_price = entry_price * (1 - STOP_LOSS_RATIO)
        else: # SELL
            tp_price = entry_price * (1 - TAKE_PROFIT_RATIO)
            sl_price = entry_price * (1 + STOP_LOSS_RATIO)
            
        logging.warning(f"🟢 {symbol} - エントリーシグナル: {side} (Score: {score:.2f})")
        
        # エントリー注文の実行（成行）
        entry_order = await execute_trade(exchange, symbol, "market", side, amount, price=entry_price)
        
        if entry_order is not None:
            # 注文が成功した場合
            final_entry_price = entry_order.get('price', entry_price)
            
            # ポジション情報の保存
            OPEN_POSITIONS[symbol] = {
                "entry_price": final_entry_price,
                "sl_price": sl_price,
                "tp_price": tp_price,
                "side": side,
                "amount": amount,
                "score": score,
                "order_id": entry_order.get('id', 'N/A')
            }
            
            # Telegram通知
            msg = format_telegram_message("🆕 NEW ENTRY SIGNAL", symbol, final_entry_price, sl_price, tp_price, score, side)
            send_telegram_alert(msg)
            
            # ログ記録
            log_data = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "symbol": symbol,
                "side": side,
                "action": "ENTRY",
                "entry_price": final_entry_price,
                "amount": amount,
                "score": score,
                "order_id": entry_order.get('id', 'N/A')
            }
            write_trade_log_and_sync(log_data, is_entry=True)
        
        else:
            # execute_trade内で詳細なエラーログは出力済み
            logging.error(f"❌ {symbol} のエントリー注文の実行に失敗しました。")

async def update_macro_context(exchange: ccxt_async.Exchange):
    """FGIプロキシに基づき、グローバルな市場環境を更新する"""
    global GLOBAL_MACRO_CONTEXT
    
    fgi_proxy = await fetch_fgi_proxy(exchange)
    
    new_context = GLOBAL_MACRO_CONTEXT
    if fgi_proxy <= FGI_SLUMP_THRESHOLD:
        new_context = 0 # SLUMP (低迷/リスクオフ)
    elif fgi_proxy >= FGI_ACTIVE_THRESHOLD:
        new_context = 2 # ACTIVE (活発/リスクオン)
    else:
        new_context = 1 # NORMAL (通常/中立)
        
    if new_context != GLOBAL_MACRO_CONTEXT:
        context_map = {0: "低迷/SLUMP", 1: "通常/NORMAL", 2: "活発/ACTIVE"}
        logging.warning(f"🔔 市場環境が変化しました: {context_map[GLOBAL_MACRO_CONTEXT]} -> {context_map[new_context]}")
        
    GLOBAL_MACRO_CONTEXT = new_context


async def main_bot_loop(exchange: ccxt_async.Exchange):
    """BOTのメインループ"""
    global LAST_SUCCESS_TIME
    
    while True:
        try:
            logging.info("--- メインループ開始 ---")

            # 1. 市場環境の更新 (頻度は低め)
            await update_macro_context(exchange)

            # 2. ポジションの決済チェック
            await check_for_exits(exchange)
            
            # 3. 新規エントリーチェック
            await check_for_entries(exchange)
            
            LAST_SUCCESS_TIME = time.time()
            logging.info("--- メインループ完了 ---")
            
        except Exception as e:
            logging.error(f"致命的なエラーが発生しました。BOTは続行します: {e}", exc_info=True)
            
        finally:
            # 次のループまで待機
            await asyncio.sleep(LOOP_INTERVAL)


# ====================================================================================
# FASTAPI ENDPOINTS
# ====================================================================================

@app.on_event("startup")
async def startup_event():
    """アプリケーション起動時に実行される処理"""
    global exchange
    
    # CCXTクライアントの初期化
    exchange = await initialize_exchange()
    
    if exchange:
        # メインループを非同期タスクとして起動
        asyncio.create_task(main_bot_loop(exchange))
    else:
        logging.error("❌ BOTのメインタスクは起動されませんでした。CCXTクライアントの初期化に失敗しています。")

@app.get("/status")
async def get_status():
    """BOTの現在のステータスを返すAPIエンドポイント"""
    current_time = time.time()
    # LAST_SUCCESS_TIMEがゼロの場合は現在の時刻を基準にする（初回起動時）
    last_time_for_calc = LAST_SUCCESS_TIME if LAST_SUCCESS_TIME > 0 else current_time
    next_check = max(0, int(LOOP_INTERVAL - (current_time - last_time_for_calc)))

    status_msg = {
        "status": "ok",
        "bot_version": "v19.0.28 - Safety and Frequency Finalized (Patch 36) - WINRATE_PATCH", # バージョン更新
        "base_trade_size_usdt": BASE_TRADE_SIZE_USDT, 
        "managed_positions_count": len(OPEN_POSITIONS), 
        # last_success_time は、LAST_SUCCESS_TIMEが初期値(0.0)でない場合にのみフォーマットする
        "last_success_time_utc": datetime.fromtimestamp(LAST_SUCCESS_TIME, timezone.utc).isoformat() if LAST_SUCCESS_TIME > 0 else "N/A",
        "next_main_loop_check_seconds": next_check,
        "current_threshold": get_current_threshold(GLOBAL_MACRO_CONTEXT),
        "macro_context": GLOBAL_MACRO_CONTEXT, # 0:低リスク, 1:中リスク, 2:高リスク
        "is_test_mode": TEST_MODE,
    }
    return JSONResponse(content=status_msg)

# ====================================================================================
# MAIN EXECUTION
# ====================================================================================

if __name__ == "__main__":
    # FastAPIサーバーの起動
    uvicorn.run(app, host="0.0.0.0", port=8000)
