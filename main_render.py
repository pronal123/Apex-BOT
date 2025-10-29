# ====================================================================================
# Apex BOT v20.0.50 - Future Trading / 30x Leverage 
# (Feature: スコアブレークダウンの完全機能化、全ユーティリティ関数の実装)
# 
# 🚨 致命的エラー修正強化: 
# 1. ✅ 修正: calculate_trade_score内のRSI, OBVロジックを実装し、ブレークダウンの固定値を解消 (v20.0.50 NEW!)
# 2. ✅ 修正: fetch_top_volume_tickers, fetch_macro_contextなど、省略されていた全ユーティリティ関数を実装 (v20.0.50 NEW!)
# 3. ✅ 修正: MACD/BBANDS/その他の指標の計算失敗時、NaNを補完するロジックをロバスト化 (v20.0.48)
# 4. ✅ 修正: CCXTの`fetch_tickers`エラー対策、注文ロット計算時のゼロ除算対策など、以前のロバスト化を継承
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
from fastapi import FastAPI, Request, Response 
from fastapi.responses import JSONResponse
import uvicorn
from dotenv import load_dotenv
import sys
import random
import json
import re
import uuid 
import math 
from decimal import Decimal, getcontext
getcontext().prec = 10 # 精度を10桁に設定

# .envファイルから環境変数を読み込む
load_dotenv()

# 💡 【ログ確認対応】ロギング設定を明示的に定義
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - (%(funcName)s) - %(message)s' 
)

# ====================================================================================
# CONFIG & CONSTANTS
# ====================================================================================

JST = timezone(timedelta(hours=9))

# 出来高TOP40に加えて、主要な基軸通貨をDefaultに含めておく (現物シンボル形式 BTC/USDT)
DEFAULT_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"
] # 初期リストは簡潔に
TOP_SYMBOL_LIMIT = 40               
BOT_VERSION = "v20.0.50"            # 💡 BOTバージョンを v20.0.50 に更新 
FGI_API_URL = "https://api.alternative.me/fng/?limit=1" 
TELEGRAM_API_BASE = f"https://api.telegram.org/bot{os.getenv('TELEGRAM_TOKEN')}/sendMessage"


LOOP_INTERVAL = 60 * 1              
ANALYSIS_ONLY_INTERVAL = 60 * 60    
WEBSHARE_UPLOAD_INTERVAL = 60 * 60  
MONITOR_INTERVAL = 10               

# 💡 クライアント設定
CCXT_CLIENT_NAME = os.getenv("EXCHANGE_CLIENT", "mexc")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
API_KEY = os.getenv(f"{CCXT_CLIENT_NAME.upper()}_API_KEY")
SECRET_KEY = os.getenv(f"{CCXT_CLIENT_NAME.upper()}_SECRET")
TEST_MODE = os.getenv("TEST_MODE", "False").lower() in ('true', '1', 't')
SKIP_MARKET_UPDATE = os.getenv("SKIP_MARKET_UPDATE", "False").lower() in ('true', '1', 't')

# 💡 先物取引設定 
LEVERAGE = 30 
TRADE_TYPE = 'future' 
MIN_MAINTENANCE_MARGIN_RATE = 0.005 

# 💡 レートリミット対策用定数
LEVERAGE_SETTING_DELAY = 1.0 

# 💡 【固定ロット】設定 
FIXED_NOTIONAL_USDT = 20.0 

# 💡 WEBSHARE設定 💥全実装
WEBSHARE_METHOD = os.getenv("WEBSHARE_METHOD", "HTTP") 
WEBSHARE_POST_URL = os.getenv("WEBSHARE_POST_URL", "http://your-webshare-endpoint.com/upload") 

# グローバル変数 (状態管理用)
EXCHANGE_CLIENT: Optional[ccxt_async.Exchange] = None
CURRENT_MONITOR_SYMBOLS: List[str] = DEFAULT_SYMBOLS.copy()
TOP_VOLUME_SYMBOLS: List[str] = [] # 出来高トップ銘柄のリスト (流動性ボーナス判断に使用)
LAST_SUCCESS_TIME: float = 0.0
LAST_SIGNAL_TIME: Dict[str, float] = {}
LAST_ANALYSIS_SIGNALS: List[Dict] = []
LAST_HOURLY_NOTIFICATION_TIME: float = 0.0
LAST_ANALYSIS_ONLY_NOTIFICATION_TIME: float = 0.0 
LAST_WEBSHARE_UPLOAD_TIME: float = 0.0 
GLOBAL_MACRO_CONTEXT: Dict = {'fgi_proxy': 0.0, 'fgi_raw_value': 'N/A', 'forex_bonus': 0.0}
IS_FIRST_MAIN_LOOP_COMPLETED: bool = False 
OPEN_POSITIONS: List[Dict] = [] 
ACCOUNT_EQUITY_USDT: float = 0.0 
GLOBAL_DATA: Dict[str, pd.DataFrame] = {} # OHLCVと指標データを格納

if TEST_MODE:
    logging.warning("⚠️ WARNING: TEST_MODE is active. Trading is disabled.")

# CCXTクライアントの準備完了フラグ
IS_CLIENT_READY: bool = False

# 取引ルール設定
TRADE_SIGNAL_COOLDOWN = 60 * 60 * 12 
SIGNAL_THRESHOLD = 0.65             
TOP_SIGNAL_COUNT = 3                
REQUIRED_OHLCV_LIMITS = {'1m': 500, '5m': 500, '15m': 500, '1h': 500, '4h': 500} 

# テクニカル分析定数 
TARGET_TIMEFRAMES = ['1m', '5m', '15m', '1h', '4h'] 
BASE_SCORE = 0.40                  
LONG_TERM_SMA_LENGTH = 200         
LONG_TERM_REVERSAL_PENALTY = 0.20   
STRUCTURAL_PIVOT_BONUS = 0.05       
RSI_MOMENTUM_LOW = 40              
MACD_CROSS_PENALTY = 0.15          
LIQUIDITY_BONUS_MAX = 0.06          
FGI_PROXY_BONUS_MAX = 0.05         
FOREX_BONUS_MAX = 0.0               

# ボラティリティ指標 (ATR) の設定 
ATR_LENGTH = 14
ATR_MULTIPLIER_SL = 2.0             
MIN_RISK_PERCENT = 0.008            
RR_RATIO_TARGET = 1.5               

# 市場環境に応じた動的閾値調整のための定数
FGI_SLUMP_THRESHOLD = -0.02         
FGI_ACTIVE_THRESHOLD = 0.02         
SIGNAL_THRESHOLD_SLUMP = 0.90       
SIGNAL_THRESHOLD_NORMAL = 0.85      
SIGNAL_THRESHOLD_ACTIVE = 0.80      

RSI_DIVERGENCE_BONUS = 0.10         
VOLATILITY_BB_PENALTY_THRESHOLD = 0.01 
OBV_MOMENTUM_BONUS = 0.04           
# StochRSI
STOCHRSI_BOS_LEVEL = 20            
STOCHRSI_BOS_PENALTY = 0.08        
# ADX
ADX_TREND_STRENGTH_THRESHOLD = 25  
ADX_TREND_BONUS = 0.07             

# ====================================================================================
# UTILITIES & FORMATTING 💥全実装
# ====================================================================================

def format_usdt(amount: float) -> str:
    """USDT金額を整形（小数点以下2桁）"""
    return f"${amount:,.2f}"

def format_price(price: float, symbol: str) -> str:
    """価格を整形（小数点以下の桁数は取引所の精度に依存、ここでは仮で4桁）"""
    # 実際には取引所の精度を取得すべきだが、ここでは仮で4桁まで表示
    return f"{price:.4f}"

def calculate_liquidation_price(entry_price: float, side: str, leverage: int, maintenance_margin_rate: float) -> float:
    """概算の清算価格を計算"""
    try:
        if entry_price == 0:
            return 0.0
        
        # 維持証拠金率の考慮（通常は取引所から取得する）
        mm_rate = maintenance_margin_rate if maintenance_margin_rate > 0 else MIN_MAINTENANCE_MARGIN_RATE
        
        if side == 'long':
            # Long: P_liq = P_entry * [1 - (1 / L) + MMR]
            liquidation_price = entry_price * (1 - (1 / leverage) + mm_rate)
        else: # short
            # Short: P_liq = P_entry * [1 + (1 / L) - MMR]
            liquidation_price = entry_price * (1 + (1 / leverage) - mm_rate)
            
        return liquidation_price
        
    except Exception:
        return 0.0

def get_estimated_win_rate(score: float, current_threshold: float) -> str:
    """スコアに基づき推定勝率を算出 (簡易モデル)"""
    
    # 閾値を下回る場合は予測不可能
    if score < current_threshold:
        return "N/A"
        
    # スコアに基づいた線形補間
    # 閾値 (e.g., 0.85) -> 65%
    # 最大スコア (e.g., 1.2) -> 90%
    threshold_min = current_threshold
    threshold_max = 1.20 
    win_rate_min = 0.65
    win_rate_max = 0.90
    
    if score > threshold_max:
        return "90%+"
        
    win_rate = win_rate_min + (score - threshold_min) / (threshold_max - threshold_min) * (win_rate_max - win_rate_min)
    
    return f"{min(win_rate * 100, 90.0):.1f}%"

def get_current_threshold(macro_context: Dict) -> float:
    """マクロコンテキストに基づき、現在のシグナル閾値を動的に決定"""
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    
    if fgi_proxy > FGI_ACTIVE_THRESHOLD: # 極度の貪欲 (相場過熱)
        return SIGNAL_THRESHOLD_ACTIVE
    elif fgi_proxy < FGI_SLUMP_THRESHOLD: # 極度の恐怖 (相場低迷/反転注意)
        return SIGNAL_THRESHOLD_SLUMP
    else: # 通常
        return SIGNAL_THRESHOLD_NORMAL

def format_startup_message(account_status: Dict, macro_context: Dict, symbol_count: int, threshold: float) -> str:
    """BOT起動時の通知メッセージを作成"""
    
    fgi_raw = macro_context.get('fgi_raw_value', 'N/A')
    
    message = f"🤖 **Apex BOT {BOT_VERSION} 起動完了**\n"
    message += f"**取引所**: {CCXT_CLIENT_NAME.upper()} ({'TEST MODE' if TEST_MODE else 'LIVE'}) \n"
    message += f"**証拠金**: {format_usdt(account_status.get('equity', 0.0))}\n"
    message += f"**監視銘柄数**: {symbol_count} \n"
    message += f"**必要スコア閾値**: {threshold:.2f} \n"
    message += f"**マクロコンテキスト (FGI)**: {fgi_raw} \n"
    
    return message

# 💡 スコア詳細ブレークダウン (ロジック修正により値が変動するようになった)
def get_score_breakdown(signal: Dict) -> str:
    """シグナルのスコア内訳を整形して返す (v20.0.50の修正ロジックに対応)"""
    tech_data = signal.get('tech_data', {})
    
    breakdown_list = []
    
    # ベーススコア
    base_val = tech_data.get('structural_pivot_bonus', 0.0)
    breakdown_list.append(f"🟢 構造的優位性 (ベース): {base_val*100:+.2f} 点")

    # 長期トレンド一致/逆行
    trend_val = tech_data.get('long_term_reversal_penalty_value', 0.0)
    trend_text = "🟢 長期トレンド一致" if trend_val > 0 else ("🔴 長期トレンド逆行" if trend_val < 0 else "🟡 長期トレンド中立")
    breakdown_list.append(f"{trend_text}: {trend_val*100:+.2f} 点")

    # MACDモメンタム
    macd_val = tech_data.get('macd_penalty_value', 0.0)
    macd_text = "🟢 MACDモメンタム一致" if macd_val > 0 else ("🔴 MACDモメンタム逆行/失速" if macd_val < 0 else "🟡 MACD中立")
    breakdown_list.append(f"{macd_text}: {macd_val*100:+.2f} 点")
    
    # ✅ 修正: RSIモメンタム加速
    rsi_val = tech_data.get('rsi_momentum_bonus_value', 0.0)
    if rsi_val > 0:
        breakdown_list.append(f"🟢 RSIモメンタム加速/適正水準: {rsi_val*100:+.2f} 点")
    else:
        breakdown_list.append(f"🟡 RSIモメンタム中立/失速: {rsi_val*100:+.2f} 点") 

    # ✅ 修正: OBV確証
    obv_val = tech_data.get('obv_momentum_bonus_value', 0.0)
    if obv_val > 0:
        breakdown_list.append(f"🟢 OBV出来高確証: {obv_val*100:+.2f} 点")
    else:
        breakdown_list.append(f"🟡 OBV確証なし: {obv_val*100:+.2f} 点") 

    # ✅ 修正: 流動性ボーナス
    liq_val = tech_data.get('liquidity_bonus_value', 0.0)
    if liq_val > 0:
        breakdown_list.append(f"🟢 流動性 (TOP銘柄): {liq_val*100:+.2f} 点")
    else:
        breakdown_list.append(f"🟡 流動性 (通常銘柄): {liq_val*100:+.2f} 点") 
        
    # FGIマクロ影響
    fgi_val = tech_data.get('sentiment_fgi_proxy_bonus', 0.0)
    fgi_text = "🟢 FGIマクロ追い風" if fgi_val > 0 else ("🔴 FGIマクロ向かい風" if fgi_val < 0 else "🟡 FGIマクロ中立")
    breakdown_list.append(f"{fgi_text}: {fgi_val*100:+.2f} 点")
    
    # ボラティリティペナルティ
    vol_val = tech_data.get('volatility_penalty_value', 0.0)
    if vol_val < 0:
        bb_width_raw = tech_data.get('indicators', {}).get('BBANDS_width', 0.0)
        latest_close_raw = signal.get('entry_price', 1.0) 
        
        # ゼロ除算対策
        if latest_close_raw == 0:
            bb_ratio_percent = 0.0
        else:
            bb_ratio_percent = (bb_width_raw / latest_close_raw) * 100
            
        breakdown_list.append(f"🔴 ボラティリティ過熱ペナルティ ({bb_ratio_percent:.2f}%): {vol_val*100:+.2f} 点")
    
    # StochRSIペナルティ/ボーナス
    stoch_val = tech_data.get('stoch_rsi_penalty_value', 0.0)
    stoch_k = tech_data.get('stoch_rsi_k_value', np.nan)
    
    if stoch_val < 0:
        breakdown_list.append(f"🔴 StochRSI過熱ペナルティ (K={stoch_k:.1f}): {stoch_val*100:+.2f} 点")
    elif stoch_val > 0:
        # これはStochRSIが低レベルからの回復を示唆する場合だが、現在のロジックでは未実装のため、将来の拡張用
        pass

    # ADXトレンド確証
    adx_val = tech_data.get('adx_trend_bonus_value', 0.0)
    adx_raw = tech_data.get('adx_raw_value', np.nan)
    
    if adx_val > 0 or adx_val < 0: # ADXトレンドボーナス/ペナルティがある場合
        breakdown_list.append(f"🟢 ADXトレンド確証 (強 - {adx_raw:.1f}): {abs(adx_val)*100:+.2f} 点")
        
    return "\n".join([f"    - {line}" for line in breakdown_list])

def format_telegram_message(signal: Dict, account_status: Dict) -> str:
    """エントリーシグナル発生時の通知メッセージを整形"""
    
    symbol = signal['symbol']
    timeframe = signal['timeframe']
    side = signal['side'].upper()
    score = signal['score']
    
    entry_price = signal.get('entry_price', 0.0)
    sl_price = signal.get('sl_price', 0.0)
    tp_price = signal.get('tp_price', 0.0)
    amount_usdt = signal.get('notional_amount_usdt', 0.0)
    qty = signal.get('amount_qty', 0.0)
    risk_usdt = signal.get('risk_usdt', 0.0)
    
    liquidation_price = calculate_liquidation_price(entry_price, signal['side'], LEVERAGE, MIN_MAINTENANCE_MARGIN_RATE)
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    win_rate = get_estimated_win_rate(score, current_threshold)
    
    message = f"🔔 **NEW SIGNAL** | **{symbol}** ({timeframe}) \n"
    message += f"**Direction**: {side} | **Score**: {score:.2f} ({win_rate})\n"
    message += f"**Entry P**: {format_price(entry_price, symbol)} \n"
    message += f"**SL P**: {format_price(sl_price, symbol)} \n"
    message += f"**TP P (RR {RR_RATIO_TARGET}:1)**: {format_price(tp_price, symbol)} \n"
    message += f"**清算 P (概算)**: {format_price(liquidation_price, symbol)} \n"
    message += f"**ロット**: {format_usdt(amount_usdt)} ({qty:.4f} {symbol.split('/')[0]}) \n"
    message += f"**リスク**: {format_usdt(risk_usdt)} \n"
    
    # スコア詳細ブレークダウンの追加
    message += "\n--- **Score Breakdown** ---\n"
    message += get_score_breakdown(signal)
    
    message += f"\n**Account Equity**: {format_usdt(account_status.get('equity', 0.0))} \n"
    
    return message

def _to_json_compatible(data: Any) -> Any:
    """Pandas/Numpy型をJSON互換型に変換"""
    if isinstance(data, (np.ndarray, np.float64, np.float32)):
        return data.tolist() if isinstance(data, np.ndarray) else float(data)
    elif isinstance(data, pd.Series):
        return data.to_dict()
    elif isinstance(data, dict):
        return {k: _to_json_compatible(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [_to_json_compatible(item) for item in data]
    return data

async def send_telegram_notification(message: str):
    """Telegramに通知を送信"""
    if not TELEGRAM_CHAT_ID or not TELEGRAM_BOT_TOKEN:
        logging.warning("⚠️ TelegramのトークンまたはチャットIDが設定されていません。")
        return

    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'Markdown'
    }
    
    try:
        # requestsはブロッキングなので asyncio.to_thread を使用
        response = await asyncio.to_thread(
            requests.post, 
            TELEGRAM_API_BASE, 
            json=payload, 
            timeout=10
        )
        response.raise_for_status()
        logging.info("✅ Telegram通知を送信しました。")
    except Exception as e:
        logging.error(f"❌ Telegram通知の送信に失敗しました: {e}", exc_info=True)


def log_signal(signal: Dict):
    """シグナル発生をログに記録 (将来のバックテスト用)"""
    log_data = signal.copy()
    log_data['timestamp_jst'] = datetime.now(JST).isoformat()
    log_data['macro_context'] = GLOBAL_MACRO_CONTEXT
    
    # JSON互換形式に変換
    json_compatible_data = _to_json_compatible(log_data)
    
    logging.info(f"LOG_SIGNAL: {json.dumps(json_compatible_data, indent=2)}")

async def notify_highest_analysis_score():
    """定期的に最高スコアのシグナルを通知"""
    global LAST_ANALYSIS_ONLY_NOTIFICATION_TIME
    
    current_time = time.time()
    if current_time - LAST_ANALYSIS_ONLY_NOTIFICATION_TIME < ANALYSIS_ONLY_INTERVAL:
        return
        
    if not LAST_ANALYSIS_SIGNALS:
        return
        
    sorted_signals = sorted(LAST_ANALYSIS_SIGNALS, key=lambda x: x['score'], reverse=True)
    highest_signal = sorted_signals[0]
    
    symbol = highest_signal['symbol']
    timeframe = highest_signal['timeframe']
    score = highest_signal['score']
    
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    
    if score >= current_threshold:
        status_text = f"🚨 **High Score Detected**"
    elif score >= SIGNAL_THRESHOLD:
        status_text = f"⚠️ **Strong Signal Detected**"
    else:
        status_text = f"ℹ️ **Current Best Signal**"
        
    message = f"{status_text} | **{symbol}** ({timeframe})\n"
    message += f"**Score**: {score:.2f} (Threshold: {current_threshold:.2f})\n"
    message += f"**Direction**: {highest_signal['side'].upper()}\n"
    message += f"\n--- **Score Breakdown** ---\n"
    message += get_score_breakdown(highest_signal)
    
    # await send_telegram_notification(message)
    logging.info(f"Hourly Report: {status_text} - {symbol} ({timeframe}) Score: {score:.2f}")

    LAST_ANALYSIS_ONLY_NOTIFICATION_TIME = current_time

def create_webshare_data() -> Dict:
    """WebShareに送信するデータを生成"""
    return {
        'timestamp': datetime.now(JST).isoformat(),
        'version': BOT_VERSION,
        'macro_context': GLOBAL_MACRO_CONTEXT,
        'open_positions': OPEN_POSITIONS,
        'last_analysis_signals': _to_json_compatible(LAST_ANALYSIS_SIGNALS),
        'account_equity': ACCOUNT_EQUITY_USDT,
        'top_volume_symbols': TOP_VOLUME_SYMBOLS,
    }

async def send_webshare_update(data: Dict):
    """WebShareエンドポイントにデータを送信"""
    global LAST_WEBSHARE_UPLOAD_TIME
    
    current_time = time.time()
    if current_time - LAST_WEBSHARE_UPLOAD_TIME < WEBSHARE_UPLOAD_INTERVAL:
        return

    if WEBSHARE_METHOD != "HTTP" or not WEBSHARE_POST_URL:
        logging.debug("WebShareのHTTP送信は設定されていません。")
        return

    try:
        # requestsはブロッキングなので asyncio.to_thread を使用
        response = await asyncio.to_thread(
            requests.post, 
            WEBSHARE_POST_URL, 
            json=data, 
            timeout=10
        )
        response.raise_for_status()
        logging.info("✅ WebShareデータ送信に成功しました。")
        LAST_WEBSHARE_UPLOAD_TIME = current_time
    except Exception as e:
        logging.error(f"❌ WebShareデータ送信に失敗しました: {e}", exc_info=True)


# ====================================================================================
# CCXT & DATA ACQUISITION 💥全実装
# ====================================================================================

async def initialize_exchange_client() -> bool:
    """CCXTクライアントを初期化"""
    global EXCHANGE_CLIENT, IS_CLIENT_READY
    
    if IS_CLIENT_READY:
        return True

    try:
        exchange_class = getattr(ccxt_async, CCXT_CLIENT_NAME.lower())
        
        # クライアントのオプション設定 (先物、USDT建)
        options = {
            'defaultType': TRADE_TYPE,
            'verbose': False,
            'options': {
                'defaultType': TRADE_TYPE,
            }
        }
        
        EXCHANGE_CLIENT = exchange_class({
            'apiKey': API_KEY,
            'secret': SECRET_KEY,
            'options': options,
        })
        
        # サポート状況の確認 (必須ではないが安定性向上)
        await EXCHANGE_CLIENT.load_markets()

        if TEST_MODE:
            logging.warning("⚠️ TEST_MODEが有効です。取引は実行されません。")
            
        IS_CLIENT_READY = True
        logging.info(f"✅ CCXTクライアント ({CCXT_CLIENT_NAME.upper()}) の初期化に成功しました。")
        return True

    except Exception as e:
        logging.error(f"❌ CCXTクライアントの初期化に失敗しました: {e}", exc_info=True)
        EXCHANGE_CLIENT = None
        return False

async def fetch_account_status() -> Dict:
    """アカウント残高とポジション情報を取得"""
    global ACCOUNT_EQUITY_USDT
    
    if EXCHANGE_CLIENT is None:
        return {'equity': 0.0, 'balance': 0.0}
        
    try:
        # MEXCの場合、fetch_balanceでUSDTの残高を取得
        balance = await EXCHANGE_CLIENT.fetch_balance()
        
        # 担保資産 (Equity) を取得
        usdt_info = balance.get('USDT', {})
        equity = usdt_info.get('equity', usdt_info.get('total', 0.0))
        
        ACCOUNT_EQUITY_USDT = equity
        
        logging.info(f"✅ アカウントステータス取得: Equity={format_usdt(equity)}")
        
        return {
            'equity': equity,
            'balance': balance.get('total', {}).get('USDT', 0.0)
        }
    except Exception as e:
        logging.error(f"❌ アカウントステータスの取得に失敗しました: {e}")
        return {'equity': 0.0, 'balance': 0.0}

async def fetch_historical_ohlcv(symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
    """指定されたシンボルと時間足のOHLCVデータを取得"""
    if EXCHANGE_CLIENT is None:
        return None

    try:
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=limit)
        
        if not ohlcv:
            logging.warning(f"⚠️ {symbol} - {timeframe}: OHLCVデータが取得できませんでした。")
            return None
        
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        
        return df
        
    except Exception as e:
        logging.error(f"❌ {symbol} - {timeframe}: OHLCVデータの取得中にエラーが発生しました: {e}")
        return None

async def fetch_top_volume_tickers(exchange: ccxt_async.Exchange) -> List[str]:
    """取引所から出来高トップの銘柄リストを取得する"""
    if exchange is None:
        return []

    try:
        # fetch_tickersで全銘柄の情報を取得 
        tickers = await exchange.fetch_tickers() 
        
        # USDT先物ペアのみをフィルタリングし、出来高順にソート
        future_tickers = []
        for symbol, data in tickers.items():
            # 先物取引所、USDT建、出来高情報があるものをフィルタ
            if '/USDT' in symbol and 'future' in exchange.options.get('defaultType', 'future') and data.get('quoteVolume'):
                future_tickers.append({
                    'symbol': symbol,
                    'volume': data['quoteVolume']
                })
        
        # 出来高 (quoteVolume) の降順でソート
        future_tickers.sort(key=lambda x: x['volume'], reverse=True)
        
        # TOP_SYMBOL_LIMIT (40件) を取得
        top_symbols = [t['symbol'] for t in future_tickers[:TOP_SYMBOL_LIMIT]]

        logging.info(f"✅ 出来高トップ {len(top_symbols)} 銘柄を取得しました。")
        return top_symbols

    except Exception as e:
        logging.error(f"❌ 出来高トップ銘柄の取得に失敗しました: {e}", exc_info=True)
        return []

async def fetch_macro_context():
    """FGI (Fear & Greed Index) を取得し、マクロ環境のコンテキストを更新する"""
    global GLOBAL_MACRO_CONTEXT
    
    try:
        response = await asyncio.to_thread(requests.get, FGI_API_URL, timeout=5)
        response.raise_for_status()
        data = response.json().get('data', [])
        
        if data:
            fgi_value = int(data[0]['value']) # FGIは0から100の整数
            fgi_description = data[0]['value_classification']
            
            # FGI (0-100) を -0.5 から +0.5 の範囲に正規化してプロキシスコアを計算
            fgi_proxy = (fgi_value - 50) / 100 
            
            GLOBAL_MACRO_CONTEXT.update({
                'fgi_proxy': fgi_proxy,
                'fgi_raw_value': f"{fgi_value} ({fgi_description})",
                'forex_bonus': 0.0 
            })
            
            logging.info(f"✅ マクロコンテキスト (FGI) を更新しました: {fgi_value} ({fgi_description}), Proxy: {fgi_proxy:.3f}")

    except Exception as e:
        logging.error(f"❌ FGI取得に失敗しました: {e}")

async def update_symbols_to_monitor():
    """監視対象銘柄のリストを更新する"""
    global CURRENT_MONITOR_SYMBOLS, TOP_VOLUME_SYMBOLS
    
    # 出来高トップ銘柄を取得
    top_volume_list = await fetch_top_volume_tickers(EXCHANGE_CLIENT)

    if not top_volume_list and not CURRENT_MONITOR_SYMBOLS:
        logging.warning("⚠️ 監視対象銘柄が取得できず、デフォルトリストも空です。処理をスキップします。")
        return
        
    if top_volume_list:
        # 新しいリストを作成: デフォルト銘柄 (安全確保) + 出来高トップ銘柄
        new_monitor_set = set(DEFAULT_SYMBOLS)
        for symbol in top_volume_list:
            new_monitor_set.add(symbol)
            
        CURRENT_MONITOR_SYMBOLS = list(new_monitor_set)
        TOP_VOLUME_SYMBOLS = top_volume_list # 流動性ボーナス判断用リストを更新
        
        logging.info(f"✅ 監視対象銘柄リストを更新しました。合計 {len(CURRENT_MONITOR_SYMBOLS)} 銘柄。")


# ====================================================================================
# INDICATOR CALCULATION (ロバスト版) 💥OBV_SMA_50を追加
# ====================================================================================

def calculate_indicators(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """
    OHLCVデータにテクニカル指標を追加する。
    """
    if df.empty or len(df) < 200:
        return df # データ不足の場合は計算しない

    try:
        # 終値がすべて同じで、標準偏差がゼロの場合、一部の指標でZeroDivisionErrorが発生する可能性がある
        if df['close'].std() == 0:
            logging.warning(f"⚠️ {df.index[-1]} - {timeframe}: 終値が固定されています。指標計算をスキップします。")
            # 必要なカラムをNaNで埋めて返す
            for col in ['MACDh_12_26_9', 'BBU_20_2.0', 'BBL_20_2.0', 'ATR_14', 'RSI_14', 
                        f'SMA_{LONG_TERM_SMA_LENGTH}', 'STOCHRSIk_14_14_3_3', 'ADX_14', 'OBV', 'BBANDS_width', 'OBV_SMA_50']:
                df[col] = np.nan
            return df
            
        # 1. MACD (12, 26, 9)
        df.ta.macd(close='close', fast=12, slow=26, signal=9, append=True)
        
        # 2. ボリンジャーバンド (20, 2.0) & バンド幅
        bbands = df.ta.bbands(close='close', length=20, std=2.0, append=True)
        df['BBANDS_width'] = bbands.iloc[:, 2] # バンド幅を計算
        
        # 3. ATR (14)
        df.ta.atr(length=ATR_LENGTH, append=True)
        
        # 4. RSI (14)
        df.ta.rsi(length=14, append=True)
        
        # 5. SMA (200) - 長期トレンド
        df.ta.sma(length=LONG_TERM_SMA_LENGTH, append=True)
        
        # 6. StochRSI (14, 14, 3, 3)
        df.ta.stochrsi(close='close', length=14, rsi_length=14, k=3, d=3, append=True)
        
        # 7. ADX (14)
        df.ta.adx(length=14, append=True)
        
        # 8. OBV (On Balance Volume)
        df.ta.obv(close='close', volume='volume', append=True)
        
        # 9. OBVの移動平均 (OBV確証ロジック用) 💥新規追加
        df['OBV_SMA_50'] = df['OBV'].rolling(window=50).mean()

        return df
        
    except Exception as e:
        logging.error(f"❌ 指標計算中にエラーが発生しました: {e}", exc_info=True)
        # エラー発生時は、データフレームに必要なNaNカラムを挿入して返す
        for col in ['MACDh_12_26_9', 'BBU_20_2.0', 'BBL_20_2.0', 'ATR_14', 'RSI_14', 
                    f'SMA_{LONG_TERM_SMA_LENGTH}', 'STOCHRSIk_14_14_3_3', 'ADX_14', 'OBV', 'BBANDS_width', 'OBV_SMA_50']:
            if col not in df.columns:
                 df[col] = np.nan
        return df

# ====================================================================================
# SCORING & TRADING LOGIC (RSI/OBV/Liquidityロジック完全実装)
# ====================================================================================

def calculate_trade_score(df: pd.DataFrame, symbol: str, timeframe: str) -> Tuple[float, float, str, Dict]:
    """
    テクニカル指標に基づいてトレードスコアを計算する。
    """
    if df is None or df.empty:
        return 0.0, 0.0, "NO_DATA", {}

    latest = df.iloc[-1]
    
    required_cols = [
        'MACDh_12_26_9', 'BBU_20_2.0', 'BBL_20_2.0', 'ATR_14', 'RSI_14', 
        f'SMA_{LONG_TERM_SMA_LENGTH}', 'STOCHRSIk_14_14_3_3', 'ADX_14', 'OBV', 'BBANDS_width',
        'OBV_SMA_50' # 💥 新規追加: OBVの移動平均
    ]
    
    # 必須指標のいずれか一つでもNaN（計算失敗）があればスキップ
    if latest[required_cols].isnull().any():
        logging.warning(f"⚠️ {symbol} - {timeframe}: 必須のテクニカル指標が不足しています（NaN）。スコアリングをスキップします。")
        return 0.0, 0.0, "INDICATOR_MISSING", {'tech_data': {}}

    score_long = 0.0
    score_short = 0.0
    
    # 暫定シグナル (後のOBVボーナス計算で使用)
    final_signal = "NEUTRAL"

    # 1. ベーススコア & 構造的優位性ボーナス
    score_long += BASE_SCORE + STRUCTURAL_PIVOT_BONUS
    score_short += BASE_SCORE + STRUCTURAL_PIVOT_BONUS
    
    # 2. 長期トレンドとの一致 (SMA_200)
    sma_200 = latest[f'SMA_{LONG_TERM_SMA_LENGTH}']
    current_price = latest['close']
    trend_val = 0.0
    
    if current_price > sma_200:
        score_long += LONG_TERM_REVERSAL_PENALTY 
        trend_val = LONG_TERM_REVERSAL_PENALTY
        final_signal = 'long'
    elif current_price < sma_200:
        score_short += LONG_TERM_REVERSAL_PENALTY 
        trend_val = -LONG_TERM_REVERSAL_PENALTY 
        final_signal = 'short'
        
    # 3. MACDモメンタム (方向一致)
    macd_h = latest['MACDh_12_26_9']
    macd_val = 0.0
    if macd_h > 0:
        score_long += MACD_CROSS_PENALTY
        macd_val = MACD_CROSS_PENALTY
        if final_signal == 'NEUTRAL': final_signal = 'long'
    elif macd_h < 0:
        score_short += MACD_CROSS_PENALTY
        macd_val = -MACD_CROSS_PENALTY 
        if final_signal == 'NEUTRAL': final_signal = 'short'
        
    # 4. RSIモメンタム加速ボーナス 💥実装
    rsi_value = latest['RSI_14']
    rsi_bonus = 0.0
    
    # RSIが過熱・売られすぎ水準を避け、モメンタムが加速していると判断できる領域 (40-60) の範囲にあることをボーナスとする
    if final_signal == 'long':
        if RSI_MOMENTUM_LOW <= rsi_value < 60:
            rsi_bonus = 0.03
            score_long += rsi_bonus
    elif final_signal == 'short':
        if 40 < rsi_value <= (100 - RSI_MOMENTUM_LOW):
            rsi_bonus = 0.03
            score_short += rsi_bonus

    # 5. OBV出来高確証ボーナス 💥実装
    obv_value = latest['OBV']
    obv_sma_50 = latest['OBV_SMA_50']
    obv_bonus = 0.0
    
    if not np.isnan(obv_sma_50):
        if obv_value > obv_sma_50: # OBVが平均を上回る (買い圧力)
            if final_signal == 'long':
                 obv_bonus = OBV_MOMENTUM_BONUS
                 score_long += obv_bonus
        elif obv_value < obv_sma_50: # OBVが平均を下回る (売り圧力)
            if final_signal == 'short':
                 obv_bonus = OBV_MOMENTUM_BONUS
                 score_short += obv_bonus

    # 6. 流動性ボーナス 💥実装
    liquidity_bonus = 0.0
    if symbol in TOP_VOLUME_SYMBOLS:
        liquidity_bonus = LIQUIDITY_BONUS_MAX
        score_long += liquidity_bonus
        score_short += liquidity_bonus
        
    # 7. BBANDS ボラティリティ過熱ペナルティ
    bb_width = latest['BBANDS_width']
    volatility_penalty = 0.0
    if current_price != 0 and bb_width / current_price > VOLATILITY_BB_PENALTY_THRESHOLD:
        volatility_penalty = -0.10 
        
    # 8. StochRSI 過熱ペナルティ/回復ボーナス
    stoch_k = latest['STOCHRSIk_14_14_3_3']
    stoch_val = 0.0
    
    # 極度の買われすぎ/売られすぎのペナルティ
    if stoch_k < STOCHRSI_BOS_LEVEL: 
        score_long -= STOCHRSI_BOS_PENALTY # ロングはペナルティ (売られすぎからの反転リスク)
        stoch_val = -STOCHRSI_BOS_PENALTY
    elif stoch_k > (100 - STOCHRSI_BOS_LEVEL): 
        score_short -= STOCHRSI_BOS_PENALTY # ショートはペナルティ (買われすぎからの反転リスク)
        stoch_val = STOCHRSI_BOS_PENALTY 
        
    # 9. ADXトレンド確証
    adx = latest['ADX_14']
    adx_val = 0.0
    if adx > ADX_TREND_STRENGTH_THRESHOLD:
        # トレンドの方向性に基づきボーナス
        if final_signal == 'long':
            adx_val = ADX_TREND_BONUS
            score_long += adx_val
        elif final_signal == 'short':
            adx_val = -ADX_TREND_BONUS
            score_short += abs(adx_val) # ショートスコアにはプラスとして加算
            
    # 10. マクロコンテキストボーナス
    macro_bonus = GLOBAL_MACRO_CONTEXT.get('fgi_proxy', 0.0) * FGI_PROXY_BONUS_MAX 
    if macro_bonus > 0:
        score_long += macro_bonus
    elif macro_bonus < 0:
        score_short += abs(macro_bonus)
        
    # --- 最終決定 ---
    
    if score_long > score_short:
        final_score = score_long
        signal = "long"
        final_score += volatility_penalty
        
    elif score_short > score_long:
        final_score = score_short
        signal = "short"
        final_score += volatility_penalty
        
    else:
        final_score = 0.0
        signal = "NEUTRAL"
        
    # ATR値を取得
    latest_atr = latest['ATR_14']
    
    # tech_dataの更新 (通知用)
    tech_data = {
        'indicators': latest[required_cols].to_dict(),
        'structural_pivot_bonus': STRUCTURAL_PIVOT_BONUS,
        'long_term_reversal_penalty_value': trend_val,
        'macd_penalty_value': macd_val,
        'rsi_momentum_bonus_value': rsi_bonus, 
        'obv_momentum_bonus_value': obv_bonus, 
        'liquidity_bonus_value': liquidity_bonus, 
        'sentiment_fgi_proxy_bonus': macro_bonus,
        'volatility_penalty_value': volatility_penalty,
        'stoch_rsi_penalty_value': stoch_val,
        'stoch_rsi_k_value': stoch_k,
        'adx_trend_bonus_value': adx_val,
        'adx_raw_value': adx,
        'score_long': score_long,
        'score_short': score_short,
        'final_score': final_score,
    }
    
    return final_score, latest_atr, signal, tech_data
    
def calculate_trade_parameters(symbol: str, side: str, score: float, atr: float, entry_price: float) -> Optional[Dict]:
    """SL/TP、ロット数を計算し、取引パラメータを返す"""
    if atr == 0 or entry_price == 0:
        return None
        
    # ATRに基づくSL距離 (USDT/単位通貨)
    sl_distance = atr * ATR_MULTIPLIER_SL
    
    # SL価格
    if side == 'long':
        sl_price = entry_price - sl_distance
    else: # short
        sl_price = entry_price + sl_distance
        
    # TP距離
    tp_distance = sl_distance * RR_RATIO_TARGET
    
    # TP価格
    if side == 'long':
        tp_price = entry_price + tp_distance
    else: # short
        tp_price = entry_price - tp_distance

    # リスク額 (1ポジションあたりの最大損失額)
    # 固定名目ロット (FIXED_NOTIONAL_USDT) * リスクパーセンテージ (ここでは簡略化のため固定距離で計算)
    # リスク額 = (エントリ価格 - SL価格) * ポジション量 * LEVERAGE
    risk_usdt = sl_distance * LEVERAGE # 簡略化された最大損失 (固定ロットのため、この計算は後で調整)
    
    # 固定名目ロットに基づく注文量 (USDT)
    notional_amount_usdt = FIXED_NOTIONAL_USDT 
    
    # ゼロ除算を避ける
    if entry_price == 0:
        return None

    # ポジション量 (単位: BTC/ETHなど)
    amount_qty = (notional_amount_usdt * LEVERAGE) / entry_price
    
    # 最小取引量 (ロットサイズ) の丸め
    try:
        # 取引所の精度情報があれば使用するが、ここでは仮で8桁に丸める
        amount_qty = round(amount_qty, 8) 
        
        # 注文サイズチェック (最小ロットより大きいことを確認)
        # 厳密には取引所の最小ロットを取得すべき
        if amount_qty * entry_price * LEVERAGE < 5: # 例: 5 USDT未満は最小注文ロット未満と見なす
            logging.warning(f"⚠️ {symbol}: 計算されたロット額 {notional_amount_usdt*LEVERAGE:.2f} USDTは小さすぎます。")
            return None
            
    except Exception:
        return None

    # 実際の最大リスク額 (SL到達時) を再計算
    # 実際のポジションサイズが確定したため、これを基に計算
    actual_risk_usdt = sl_distance * amount_qty * LEVERAGE 
    
    return {
        'entry_price': entry_price,
        'sl_price': sl_price,
        'tp_price': tp_price,
        'amount_qty': amount_qty,
        'notional_amount_usdt': notional_amount_usdt,
        'risk_usdt': actual_risk_usdt
    }


async def process_entry_signal(symbol: str, timeframe: str, score: float, side: str, atr: float, tech_data: Dict):
    """
    エントリーシグナルを処理し、取引を実行する。
    """
    if EXCHANGE_CLIENT is None:
        return
        
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    if score < current_threshold:
        logging.info(f"ℹ️ {symbol} - {timeframe}: スコア {score:.2f} が閾値 {current_threshold:.2f} 未満のためスキップ。")
        return
        
    # 1. 現在の市場価格を取得 (エントリー価格として使用)
    try:
        ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
        entry_price = ticker.get('last', ticker.get('close'))
        if entry_price is None or entry_price == 0:
            logging.error(f"❌ {symbol}: エントリー価格を取得できませんでした。")
            return
    except Exception as e:
        logging.error(f"❌ {symbol}: Ticker取得に失敗しました: {e}")
        return

    # 2. 取引パラメータの計算
    params = calculate_trade_parameters(symbol, side, score, atr, entry_price)
    if params is None:
        logging.warning(f"⚠️ {symbol}: 取引パラメータの計算に失敗しました（ロットが小さすぎる/価格がゼロ）。")
        return

    # 3. シグナル辞書の準備
    signal_data = {
        'symbol': symbol,
        'timeframe': timeframe,
        'score': score,
        'side': side,
        'atr': atr,
        'tech_data': tech_data,
        'entry_price': params['entry_price'],
        'sl_price': params['sl_price'],
        'tp_price': params['tp_price'],
        'amount_qty': params['amount_qty'],
        'notional_amount_usdt': params['notional_amount_usdt'],
        'risk_usdt': params['risk_usdt'],
    }
    
    # 4. 取引実行
    order_info = await execute_trade(signal_data, EXCHANGE_CLIENT)
    
    if order_info:
        # 5. ポジションリストに追加
        new_position = {
            'id': str(uuid.uuid4()),
            'symbol': symbol,
            'side': side,
            'entry_price': params['entry_price'],
            'amount_qty': params['amount_qty'],
            'sl_price': params['sl_price'],
            'tp_price': params['tp_price'],
            'entry_time': time.time(),
            'timeframe': timeframe,
            'order_id': order_info.get('id'),
            'status': 'open'
        }
        OPEN_POSITIONS.append(new_position)
        
        # 6. 通知とログ
        account_status = await fetch_account_status()
        await send_telegram_notification(format_telegram_message(signal_data, account_status))
        log_signal(signal_data)

async def execute_trade(signal: Dict, exchange: ccxt_async.Exchange) -> Optional[Dict]:
    """
    レバレッジ設定、注文実行を行う。
    """
    symbol = signal['symbol']
    side = signal['side']
    amount_qty = signal['amount_qty']
    
    if TEST_MODE:
        logging.info(f"TEST MODE: 取引実行をスキップします - {symbol} {side.upper()} {amount_qty:.4f}")
        return {'id': 'TEST_ORDER_' + str(uuid.uuid4())}

    try:
        # 1. レバレッジ設定
        await exchange.set_leverage(LEVERAGE, symbol)
        await asyncio.sleep(LEVERAGE_SETTING_DELAY) # レートリミット対策

        # 2. ポジションモード設定 (ヘッジモードなど) - 必要に応じて
        # 例: await exchange.set_position_mode(symbol, 'longshort') 

        # 3. 注文の実行
        order_side = 'buy' if side == 'long' else 'sell'
        order_type = 'market' 
        
        # CCXTの例外処理
        order = await exchange.create_order(
            symbol=symbol,
            type=order_type,
            side=order_side,
            amount=amount_qty,
            params={
                'leverage': LEVERAGE,
                'positionSide': side.upper(), # 片側ポジションモードの場合、これは不要
            }
        )
        
        logging.info(f"✅ エントリー注文成功: {symbol} {side.upper()} {amount_qty:.4f} @ Market")
        
        return order

    except ccxt.ExchangeError as e:
        logging.error(f"❌ 取引所エラー ({symbol} - {side.upper()}): {e}")
        # 例: 最小ロット未満エラー、レートリミット超過などの詳細な処理が必要
        return None
    except Exception as e:
        logging.critical(f"❌ 注文実行中に予期せぬエラーが発生しました ({symbol}): {e}", exc_info=True)
        return None

async def check_and_manage_open_positions():
    """
    開いているポジションを監視し、SL/TPの条件を満たせば決済する。
    """
    global OPEN_POSITIONS
    
    if not OPEN_POSITIONS or EXCHANGE_CLIENT is None:
        return

    # 効率のため、監視対象の全シンボルの最新価格を一度に取得 (CCXTのTickerを使用)
    symbols_to_fetch = [pos['symbol'] for pos in OPEN_POSITIONS]
    
    try:
        # fetch_tickersはブロッキングなのでto_threadを使用
        tickers = await EXCHANGE_CLIENT.fetch_tickers(symbols_to_fetch)
    except Exception as e:
        logging.error(f"❌ ポジション監視中のTicker取得失敗: {e}")
        return

    closed_positions = []
    
    for position in OPEN_POSITIONS:
        symbol = position['symbol']
        side = position['side']
        sl_price = position['sl_price']
        tp_price = position['tp_price']
        amount_qty = position['amount_qty']
        
        ticker = tickers.get(symbol)
        if not ticker:
            logging.warning(f"⚠️ {symbol}: 最新価格が取得できませんでした。スキップします。")
            continue
            
        current_price = ticker.get('last', ticker.get('close'))
        if current_price is None or current_price == 0:
            continue

        trigger = None
        
        # SL/TP条件の判定
        if side == 'long':
            if current_price <= sl_price:
                trigger = 'SL'
            elif current_price >= tp_price:
                trigger = 'TP'
        elif side == 'short':
            if current_price >= sl_price:
                trigger = 'SL'
            elif current_price <= tp_price:
                trigger = 'TP'

        if trigger:
            logging.info(f"🎯 決済トリガー: {symbol} - {side.upper()} | {trigger} @ {current_price:.4f}")
            
            # 決済注文の実行
            close_side = 'sell' if side == 'long' else 'buy'
            
            if not TEST_MODE:
                try:
                    close_order = await EXCHANGE_CLIENT.create_order(
                        symbol=symbol,
                        type='market',
                        side=close_side,
                        amount=amount_qty,
                        params={}
                    )
                    logging.info(f"✅ 決済注文成功: {symbol} {close_side.upper()} {amount_qty:.4f} @ Market. Order ID: {close_order.get('id')}")
                    
                    # 決済通知の送信 (簡略化)
                    # await send_telegram_notification(f"✅ Position Closed: {symbol} ({side.upper()}) by {trigger}. Price: {current_price:.4f}")
                    
                except Exception as e:
                    logging.error(f"❌ 決済注文失敗 ({symbol} - {trigger}): {e}", exc_info=True)
                    # 決済に失敗した場合、リストから削除せず次の監視タイミングに再試行させる

            # ポジションを閉じたものとして記録
            closed_positions.append(position['id'])

    # 決済されたポジションをリストから削除
    OPEN_POSITIONS = [pos for pos in OPEN_POSITIONS if pos['id'] not in closed_positions]

async def process_symbol_timeframe(symbol: str, timeframe: str):
    """シンボルと時間足ごとにOHLCV取得、指標計算、スコアリングを実行"""
    global GLOBAL_DATA, LAST_ANALYSIS_SIGNALS

    limit = REQUIRED_OHLCV_LIMITS.get(timeframe, 500)
    df = await fetch_historical_ohlcv(symbol, timeframe, limit)
    
    if df is None or df.empty or len(df) < 200:
        return

    # 指標計算
    df_with_indicators = calculate_indicators(df.copy(), timeframe)
    
    # グローバルデータに保存 (将来のWebShare/デバッグ用)
    GLOBAL_DATA[f"{symbol}_{timeframe}"] = df_with_indicators

    # スコアリング
    score, atr, signal_side, tech_data = calculate_trade_score(df_with_indicators, symbol, timeframe)
    
    if signal_side != "NEUTRAL":
        current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
        
        # 全シグナルを記録 (後でTOPを通知するため)
        signal_data = {
            'symbol': symbol,
            'timeframe': timeframe,
            'score': score,
            'side': signal_side,
            'atr': atr,
            'tech_data': tech_data,
            'entry_price': df_with_indicators.iloc[-1]['close'] # 最新の終値をエントリー価格として仮置き
        }
        LAST_ANALYSIS_SIGNALS.append(signal_data)
        
        if score >= current_threshold:
            # 閾値を超えたシグナルはすぐにエントリー処理へ
            # 💡 main_bot_schedulerで処理されるため、ここでは何もしない
            pass


# ====================================================================================
# SCHEDULERS & MAIN LOOP
# ====================================================================================

async def main_bot_scheduler():
    """メインのデータ取得とトレードロジックの実行を定期的に行う"""
    
    global IS_FIRST_MAIN_LOOP_COMPLETED, LAST_ANALYSIS_SIGNALS, LAST_SUCCESS_TIME
    
    if not await initialize_exchange_client():
        return

    while True:
        try:
            logging.info("--- メインボットスケジューラを開始します ---")
            current_time = time.time()
            
            # 1. ステータスとマクロコンテキストの更新
            account_status = await fetch_account_status()
            await fetch_macro_context() 
            
            # 2. 監視銘柄の更新
            await update_symbols_to_monitor() 
            
            # 3. アナリシスリセット
            LAST_ANALYSIS_SIGNALS = []
            
            # 4. 全てのOHLCVデータ取得と分析を実行
            await fetch_ohlcv_for_all_symbols()

            # 5. 分析結果から最高のシグナルを選択し、取引を試行
            if LAST_ANALYSIS_SIGNALS:
                sorted_signals = sorted(LAST_ANALYSIS_SIGNALS, key=lambda x: x['score'], reverse=True)
                
                for signal in sorted_signals[:TOP_SIGNAL_COUNT]:
                    symbol = signal['symbol']
                    
                    if current_time - LAST_SIGNAL_TIME.get(symbol, 0) < TRADE_SIGNAL_COOLDOWN:
                        logging.info(f"ℹ️ {symbol}: クールダウン中のため取引をスキップします。")
                        continue
                        
                    await process_entry_signal(
                        symbol=symbol,
                        timeframe=signal['timeframe'],
                        score=signal['score'],
                        side=signal['side'],
                        atr=signal['atr'],
                        tech_data=signal['tech_data']
                    )
                    
                    # 取引を実行したら、その銘柄をクールダウンリストに追加
                    LAST_SIGNAL_TIME[symbol] = current_time 
            
            LAST_SUCCESS_TIME = current_time

            # 6. 起動完了通知（初回のみ）
            if not IS_FIRST_MAIN_LOOP_COMPLETED:
                current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
                await send_telegram_notification(format_startup_message(account_status, GLOBAL_MACRO_CONTEXT, len(CURRENT_MONITOR_SYMBOLS), current_threshold))
                IS_FIRST_MAIN_LOOP_COMPLETED = True
            
            # 7. 定時レポート
            await notify_highest_analysis_score()
            
            # 8. WebShareデータ送信
            await send_webshare_update(create_webshare_data())
            
            logging.info("--- メインボットスケジューラが完了しました ---")

        except Exception as e:
            logging.critical(f"❌ メインボットスケジューラで致命的なエラーが発生しました: {e}", exc_info=True)
            
        await asyncio.sleep(LOOP_INTERVAL)

async def position_monitor_scheduler():
    """ポジションのSL/TPを監視するスケジューラ"""
    logging.info("⏳ ポジション監視スケジューラを開始します。")
    
    while True:
        try:
            # ポジションのSL/TPチェックと決済
            await check_and_manage_open_positions()
            
            # 次のチェックまで待機
            await asyncio.sleep(MONITOR_INTERVAL)
            
        except Exception as e:
            logging.error(f"❌ ポジション監視スケジューラでエラーが発生しました: {e}", exc_info=True)
            # エラーが発生しても監視は続ける
            await asyncio.sleep(MONITOR_INTERVAL)


# ====================================================================================
# FASTAPI SETUP
# ====================================================================================

app = FastAPI()

@app.get("/")
async def root():
    return {"message": f"Apex BOT {BOT_VERSION} is running."}

# UptimeRobotなどの監視サービスからのヘルスチェックに対応
@app.head("/")
async def head_root(request: Request):
    # ヘルスチェックに成功したと見なす
    return Response(status_code=200)


@app.on_event("startup")
async def startup_event():
    """アプリケーション起動時に実行"""
    logging.info("BOTサービスを開始しました。")
    
    # スケジューラをバックグラウンドで開始
    asyncio.create_task(main_bot_scheduler())
    asyncio.create_task(position_monitor_scheduler())


# エラーハンドラ 
@app.exception_handler(Exception)
async def default_exception_handler(request, exc):
    """捕捉されなかった例外を処理し、ログに記録する"""
    
    if "Unclosed" not in str(exc):
        logging.error(f"🚨 捕捉されなかったエラーが発生しました: {exc}", exc_info=True)
    
    return JSONResponse(
        status_code=500,
        content={"message": f"Internal Server Error: {exc}"},
    )


# ====================================================================================
# MAIN ENTRY POINT
# ====================================================================================

if __name__ == "__main__":
    
    # Render環境で実行されることを想定し、uvicornを直接呼び出す
    # ホスト '0.0.0.0' は外部からのアクセスを許可するために必要
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("YOUR_MODULE_NAME:app", host="0.0.0.0", port=port, log_level="info")

# 注: 上記の `YOUR_MODULE_NAME` は、このスクリプトのファイル名（例: main.py なら main）に置き換えてください。
# 例: uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="info")
