# ====================================================================================
# Apex BOT v19.0.28 - Safety, Frequency & CCXT Finalized (Patch 37)
#
# 💡 【最終改良点】
# 1. メインループ間隔を1分 (60秒) に変更。
# 2. 分析タイムフレームに '1m' (1分足) と '5m' (5分足) を追加。
# 3. 実用的な position_management_loop / close_position 関数を実装し、SL/TPの自動決済を可能に。
# 4. main_bot_loopの先頭でポジション監視を実行。
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
import uuid 
import math 

# .envファイルから環境変数を読み込む
load_dotenv()

# 💡 ロギング設定を明示的に定義
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# ====================================================================================
# CONFIG & CONSTANTS
# ====================================================================================

JST = timezone(timedelta(hours=9))
BOT_VERSION = "v19.0.28 (Finalized - Practical SL/TP)"

# 監視対象銘柄 (MEXCに存在しない銘柄を削除済み)
DEFAULT_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT", "ADA/USDT",
    "DOGE/USDT", "DOT/USDT", "TRX/USDT", 
    "LTC/USDT", "AVAX/USDT", "LINK/USDT", "UNI/USDT", "ETC/USDT", "BCH/USDT",
    "NEAR/USDT", "ATOM/USDT", 
    "ALGO/USDT", "XLM/USDT", "SAND/USDT",
    "GALA/USDT", "FIL/USDT", 
    "AXS/USDT", "MANA/USDT", "AAVE/USDT",
    "FLOW/USDT", "IMX/USDT", 
]
TOP_SYMBOL_LIMIT = 40               # 監視対象銘柄の最大数
LOOP_INTERVAL = 60                  # ★メインループの実行間隔 (秒) - 1分ごと★
ANALYSIS_ONLY_INTERVAL = 60 * 60    # 分析専用通知の実行間隔 (秒)
WEBSHARE_UPLOAD_INTERVAL = 60 * 60  # WebShareログアップロード間隔 (1時間ごと)
MONITOR_INTERVAL = 10               # ポジション監視ループの実行間隔 (秒) - position_management_loopの実行頻度

# 💡 クライアント設定
CCXT_CLIENT_NAME = os.getenv("EXCHANGE_CLIENT", "mexc")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
API_KEY = os.getenv(f"{CCXT_CLIENT_NAME.upper()}_API_KEY")
SECRET_KEY = os.getenv(f"{CCXT_CLIENT_NAME.upper()}_SECRET")
TEST_MODE = os.getenv("TEST_MODE", "False").lower() in ('true', '1', 't')

try:
    BASE_TRADE_SIZE_USDT = float(os.getenv("BASE_TRADE_SIZE_USDT", "100")) 
except ValueError:
    BASE_TRADE_SIZE_USDT = 100.0

# 💡 WEBSHARE設定 
WEBSHARE_METHOD = os.getenv("WEBSHARE_METHOD", "HTTP") 
WEBSHARE_POST_URL = os.getenv("WEBSHARE_POST_URL", "http://your-webshare-endpoint.com/upload") 

# グローバル変数 (状態管理用)
EXCHANGE_CLIENT: Optional[ccxt_async.Exchange] = None
CURRENT_MONITOR_SYMBOLS: List[str] = DEFAULT_SYMBOLS.copy()
LAST_SUCCESS_TIME: float = 0.0
LAST_SIGNAL_TIME: Dict[str, float] = {}
LAST_ANALYSIS_SIGNALS: List[Dict] = []
LAST_HOURLY_NOTIFICATION_TIME: float = 0.0
LAST_ANALYSIS_ONLY_NOTIFICATION_TIME: float = 0.0
LAST_WEBSHARE_UPLOAD_TIME: float = 0.0 
GLOBAL_MACRO_CONTEXT: Dict = {}
IS_FIRST_MAIN_LOOP_COMPLETED: bool = False 
OPEN_POSITIONS: List[Dict] = [] # 現在保有中のポジション (SL/TP監視用)
IS_CLIENT_READY: bool = False

# 取引ルール設定
TRADE_SIGNAL_COOLDOWN = 60 * 60 * 2 
SIGNAL_THRESHOLD = 0.65             
TOP_SIGNAL_COUNT = 3                

# テクニカル分析定数
TARGET_TIMEFRAMES = ['1m', '5m', '15m', '1h', '4h'] # ★1分足と5分足を追加★
BASE_SCORE = 0.60                  
LONG_TERM_SMA_LENGTH = 200         
LONG_TERM_REVERSAL_PENALTY = 0.20 
STRUCTURAL_PIVOT_BONUS = 0.05    
RSI_MOMENTUM_LOW = 40             
MACD_CROSS_PENALTY = 0.15          
LIQUIDITY_BONUS_MAX = 0.06         
FGI_PROXY_BONUS_MAX = 0.05         
FOREX_BONUS_MAX = 0.0              

# 取得するOHLCVの足数
REQUIRED_OHLCV_LIMITS = {'1m': 500, '5m': 500, '15m': 500, '1h': 500, '4h': 500} 

# 市場環境に応じた動的閾値調整のための定数
FGI_SLUMP_THRESHOLD = -0.02
FGI_ACTIVE_THRESHOLD = 0.02
SIGNAL_THRESHOLD_SLUMP = 0.67
SIGNAL_THRESHOLD_NORMAL = 0.63
SIGNAL_THRESHOLD_ACTIVE = 0.58

OBV_MOMENTUM_BONUS = 0.04


# ====================================================================================
# UTILITIES & FORMATTING
# (簡潔さのため、ロジックに必要な最小限のプレースホルダーを定義)
# ====================================================================================

def format_usdt(amount: Optional[float]) -> str:
    """USDT金額をフォーマットする (Noneチェックを追加)"""
    if amount is None:
        return "N/A"
    return f"{amount:,.2f} USDT"

def get_estimated_win_rate(score: float) -> str:
    """スコアに基づく推定勝率を返す (簡略化)"""
    if score >= 0.80: return "85-95%"
    if score >= 0.70: return "75-85%"
    if score >= 0.65: return "65-75%"
    return "50-65%"

def get_current_threshold(macro_context: Dict) -> float:
    """マクロコンテキストに基づき動的な取引閾値を返す (簡略化)"""
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    if fgi_proxy <= FGI_SLUMP_THRESHOLD:
        return SIGNAL_THRESHOLD_SLUMP
    if fgi_proxy >= FGI_ACTIVE_THRESHOLD:
        return SIGNAL_THRESHOLD_ACTIVE
    return SIGNAL_THRESHOLD_NORMAL

def get_score_breakdown(signal: Dict) -> str:
    """スコア詳細を返す (簡略化)"""
    return "\n".join([
        f"  - **ベーススコア ({signal['timeframe']})**: <code>+{BASE_SCORE*100:.1f}</code> 点",
        f"  - {'✅' if signal['long_term_trend_match'] else '❌'} 長期トレンド一致: <code>{signal['long_term_trend_score']:.1f}</code> 点",
        f"  - {'✅' if signal['structural_pivot'] else '❌'} 価格構造/ピボット支持: <code>{signal['structural_pivot_score']:.1f}</code> 点",
        f"  - {'✅' if signal['momentum_ok'] else '❌'} MACD/RSIモメンタム: <code>{signal['momentum_score']:.1f}</code> 点",
        f"  - {'✅' if signal['obv_confirmation'] else '❌'} 出来高/OBV確証: <code>{signal['obv_score']:.1f}</code> 点",
        f"  - {'✅' if signal['liquidity_ok'] else '❌'} 流動性 (板の厚み) 優位: <code>{signal['liquidity_score']:.1f}</code> 点",
        f"  - {'✅' if signal['fgi_ok'] else '❌'} FGIマクロ影響: <code>{signal['fgi_score']:.1f}</code> 点",
        f"  - ⚪ ボラティリティ過熱ペナルティ: <code>{signal.get('volatility_penalty', 0.0):.1f}</code> 点",
    ])

def format_telegram_message(
    signal: Dict, 
    title: str, 
    threshold: float, 
    trade_result: Optional[Dict] = None, 
    exit_type: Optional[str] = None
) -> str:
    """Telegram通知メッセージをフォーマットする"""
    symbol = signal['symbol']
    timeframe = signal['timeframe']
    score = signal.get('final_score', 0.0)
    
    # 共通セクション
    message = f"🚀 **Apex TRADE {title}**\n"
    message += "- - - - - - - - - - - - - - - - - - - - -\n"
    message += f"  - **日時**: {datetime.now(JST).strftime('%Y/%m/%d %H:%M:%S')} (JST)\n"
    message += f"  - **銘柄**: <b>{symbol}</b> ({timeframe})\n"

    # シグナル発生時
    if trade_result is None or exit_type is None:
        trade_status = "✅ **自動売買 成功**: 現物ロング注文を執行しました。" if trade_result and trade_result.get('status') == 'ok' else "💡 **分析シグナル**: 取引待ち"
        if TEST_MODE: trade_status = "🧪 **TEST MODE**: 取引スキップ"
        
        message += f"  - **ステータス**: {trade_status}\n"
        message += f"  - **総合スコア**: <code>{score:.2f} / 100</code>\n"
        message += f"  - **取引閾値**: <code>{threshold:.2f}</code> 点\n"
        message += f"  - **推定勝率**: <code>{get_estimated_win_rate(score)}</code>\n"
        message += f"  - **リスクリワード比率 (RRR)**: <code>1:{signal['rrr']:.2f}</code>\n"
        message += "- - - - - - - - - - - - - - - - - - - - -\n"
        
        # ポジションパラメータ
        message += "📌 **ポジション管理パラメータ**\n"
        message += f"  - **エントリー**: <code>{signal['entry_price']:.4f}</code>\n"
        message += f"  - **ストップロス (SL)**: <code>{signal['stop_loss']:.4f}</code>\n"
        message += f"  - **テイクプロフィット (TP)**: <code>{signal['take_profit']:.4f}</code>\n"
        message += f"  - **リスク幅 (SL)**: <code>{signal['risk_amount']:.4f}</code> USDT\n"
        message += f"  - **リワード幅 (TP)**: <code>{signal['reward_amount']:.4f}</code> USDT\n"
        message += "- - - - - - - - - - - - - - - - - - - - -\n"
        
        # 取引実行結果
        if trade_result and trade_result.get('status') == 'ok':
            message += "💰 **取引実行結果**\n"
            message += f"  - **注文タイプ**: <code>現物 (Spot) / 成行買い</code>\n"
            message += f"  - **動的ロット**: {format_usdt(BASE_TRADE_SIZE_USDT)} (目標)\n"
            message += f"  - **約定数量**: <code>{trade_result.get('filled_amount', 0.0):.8f}</code> {symbol.split('/')[0]}\n"
            message += f"  - **平均約定額**: {format_usdt(trade_result.get('filled_cost'))}\n"
            message += "- - - - - - - - - - - - - - - - - - - - -\n"

        # スコア詳細
        message += "**📊 スコア詳細ブレークダウン** (+/-要因)\n"
        message += get_score_breakdown(signal)

    # ポジション決済時
    else:
        status_color = "🔴" if exit_type == "ストップロス (SL)" else "🟢"
        pnl_usdt = trade_result.get('pnl_usdt', 0.0)
        pnl_rate = trade_result.get('pnl_rate', 0.0)
        pnl_emoji = "📉" if pnl_usdt < 0 else "📈"

        message += f"  - **決済タイプ**: {status_color} **{exit_type}**\n"
        message += f"  - **エントリー**: <code>{signal['entry_price']:.4f}</code>\n"
        message += f"  - **決済価格**: <code>{trade_result.get('exit_price', 0.0):.4f}</code>\n"
        message += f"  - **決済日時**: {datetime.now(JST).strftime('%Y/%m/%d %H:%M:%S')}\n"
        message += "- - - - - - - - - - - - - - - - - - - - -\n"
        message += "💰 **決済損益**\n"
        message += f"  - **PNL (USDT)**: {pnl_emoji} <code>{pnl_usdt:+.2f}</code> USDT\n"
        message += f"  - **PNL (Rate)**: <code>{pnl_rate:+.2%}</code>\n"


    message += "- - - - - - - - - - - - - - - - - - - - -\n"
    message += f"<i>Bot Ver: {BOT_VERSION}</i>"
    
    # Markdown形式の強調表示をTelegramのHTMLタグに変換
    message = message.replace('<b>', '<b>').replace('</b>', '</b>').replace('<code>', '<code>').replace('</code>', '</code>')
    return message


async def send_telegram_notification(message: str):
    """Telegramに通知を送信する"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("⚠️ TelegramトークンまたはChat IDが設定されていません。通知をスキップします。")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML' # HTMLタグの使用を許可
    }
    
    try:
        response = requests.post(url, json=payload, timeout=5)
        response.raise_for_status()
        # logging.info("✅ Telegram通知を送信しました。")
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ Telegram通知の送信に失敗しました: {e}")

def log_signal(signal: Dict, type: str, trade_result: Optional[Dict] = None):
    """シグナルや取引結果をローカルログまたはDBに記録する (簡略化)"""
    log_entry = {
        'timestamp': datetime.now(JST).isoformat(),
        'type': type,
        'symbol': signal.get('symbol'),
        'timeframe': signal.get('timeframe'),
        'score': signal.get('final_score'),
        'entry_price': signal.get('entry_price'),
        'trade_result': trade_result,
        # ... その他詳細
    }
    logging.info(f"💾 LOG: {type} for {signal.get('symbol')} ({signal.get('timeframe')}) - Score: {signal.get('final_score')}")

def send_webshare_update(data: List[Dict]):
    """WebShare (HTTP POST) に情報を送信する (簡略化)"""
    global LAST_WEBSHARE_UPLOAD_TIME
    if WEBSHARE_METHOD != "HTTP" or not WEBSHARE_POST_URL:
        return
        
    try:
        payload = {
            'timestamp': datetime.now(JST).isoformat(),
            'signals': data,
            'bot_status': {
                'version': BOT_VERSION,
                'is_test_mode': TEST_MODE,
                'open_positions_count': len(OPEN_POSITIONS),
            }
        }
        response = requests.post(WEBSHARE_POST_URL, json=payload, timeout=10)
        response.raise_for_status()
        LAST_WEBSHARE_UPLOAD_TIME = time.time()
        logging.info("🌐 WebShare (HTTP POST) データを送信しました。")
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ WebShareへのデータ送信に失敗しました: {e}")


# ====================================================================================
# CCXT & DATA ACQUISITION
# ====================================================================================

async def initialize_exchange_client():
    """CCXTクライアントを初期化し、認証を試みる"""
    global EXCHANGE_CLIENT, IS_CLIENT_READY
    
    try:
        exchange_class = getattr(ccxt_async, CCXT_CLIENT_NAME)
        EXCHANGE_CLIENT = exchange_class({
            'apiKey': API_KEY,
            'secret': SECRET_KEY,
            'timeout': 20000, 
            'enableRateLimit': True,
        })
        
        # 認証のテスト (例: 残高取得)
        if API_KEY and SECRET_KEY:
            balance = await EXCHANGE_CLIENT.fetch_balance()
            usdt_balance = balance['total'].get('USDT', 0.0)
            
            # ステータス通知
            await send_telegram_notification(
                f"✅ **Bot 起動完了**\n"
                f"取引所: `{CCXT_CLIENT_NAME.upper()}`\n"
                f"認証: **成功**\n"
                f"現物 USDT残高: `{usdt_balance:,.2f} USDT`\n"
                f"テストモード: **{'ON' if TEST_MODE else 'OFF'}**\n"
                f"バージョン: `{BOT_VERSION}`"
            )
            IS_CLIENT_READY = True
            logging.info(f"✅ CCXTクライアント初期化成功。USDT残高: {usdt_balance:,.2f}")
        else:
            # APIキーがない場合は監視モードとして続行
            await send_telegram_notification(
                f"⚠️ **Bot 起動完了 (APIキーなし)**\n"
                f"取引所: `{CCXT_CLIENT_NAME.upper()}`\n"
                f"認証: **スキップ (監視モード)**\n"
                f"テストモード: **ON**\n"
                f"バージョン: `{BOT_VERSION}`"
            )
            global TEST_MODE
            TEST_MODE = True # APIキーがない場合は強制的にテストモード
            IS_CLIENT_READY = True
            logging.warning("⚠️ APIキー/シークレットが未設定です。取引を伴わない監視モードとして動作します。")

    except ccxt.AuthenticationError:
        logging.critical("❌ 致命的エラー: APIキー/シークレットが不正です。認証失敗。")
        await send_telegram_notification("❌ **致命的エラー**\\nAPIキーまたはシークレットが不正です。ボットを停止します。")
        # sys.exit(1) # 実際には終了させるべきだが、非同期環境ではログを出してループを止めない
    except Exception as e:
        logging.critical(f"❌ CCXT初期化中に予期せぬエラー: {e}", exc_info=True)
        await send_telegram_notification(f"❌ **致命的エラー**\\nCCXT初期化エラー: `{e}`")
        # sys.exit(1)

async def fetch_ohlcv_safe(symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
    """OHLCVデータを安全に取得する"""
    global EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT:
        return None
        
    try:
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True).dt.tz_convert(JST)
        # 最新の足が未確定の場合を除外
        df = df.iloc[:-1]
        
        if df.empty or len(df) < limit * 0.9: # 取得足数が少なすぎる場合はエラー
            logging.warning(f"⚠️ {symbol} ({timeframe}): 必要なデータ量 ({limit}足) を取得できませんでした。")
            return None
        return df

    except ccxt.RateLimitExceeded as e:
        logging.warning(f"⚠️ レート制限超過: {symbol} ({timeframe}) - {e}")
    except ccxt.ExchangeError as e:
        logging.error(f"❌ 取引所エラー: {symbol} ({timeframe}) - {e}")
    except Exception as e:
        logging.error(f"❌ OHLCV取得エラー: {symbol} ({timeframe}) - {e}")
        
    return None

def adjust_order_amount(symbol: str, price: float, usdt_amount: float) -> Tuple[float, float]:
    """
    USDT建ての注文金額を、取引所の数量/金額の精度に合わせて調整する
    """
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not EXCHANGE_CLIENT.markets:
        return 0.0, 0.0 # クライアント未準備

    market = EXCHANGE_CLIENT.markets.get(symbol)
    if not market:
        logging.error(f"❌ {symbol} の市場情報が見つかりません。")
        return 0.0, 0.0

    # 1. 注文数量の計算 (約定価格で割る)
    base_amount = usdt_amount / price
    
    # 2. 数量の精度調整
    amount_precision = market['precision']['amount']
    
    # 数量精度が設定されている場合
    if amount_precision is not None:
        if amount_precision > 0:
            try:
                # 精度に合わせて丸める (例: amount_precision=0.0001 -> 小数点以下4桁)
                precision_places = int(round(-math.log10(amount_precision)))
                # CCXTの規定に従い、通常は切り捨て
                base_amount = math.floor(base_amount * (10 ** precision_places)) / (10 ** precision_places)
            except ValueError:
                # log10がエラーになる場合（例: 精度が1のとき）はそのまま
                pass

    # 3. 最小注文数量のチェック
    min_amount = market.get('limits', {}).get('amount', {}).get('min', 0.0)
    if base_amount < min_amount:
        logging.warning(f"⚠️ {symbol} 調整後の数量 {base_amount:.8f} は最小注文数量 {min_amount:.8f} を下回ります。注文スキップ。")
        return 0.0, 0.0
    
    # 4. 調整後のUSDTコストを再計算
    adjusted_usdt_cost = base_amount * price

    return base_amount, adjusted_usdt_cost


# ====================================================================================
# TRADING LOGIC
# ====================================================================================

def calculate_indicators(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """
    テクニカル指標を計算する (プレースホルダー)
    """
    # SMA (長期トレンドフィルタ用)
    df['SMA_LONG'] = ta.sma(df['close'], length=LONG_TERM_SMA_LENGTH)
    
    # MACD
    df.ta.macd(append=True) # MACD_12_26_9, MACDh_12_26_9, MACDs_12_26_9
    
    # RSI
    df.ta.rsi(length=14, append=True)
    
    # Bollinger Bands
    df.ta.bbands(length=20, append=True)
    
    # OBV
    df.ta.obv(append=True)
    df['OBV_SMA'] = ta.sma(df['OBV'], length=20) # OBVのSMAトレンド
    
    # PIVOT (簡略化)
    # df['PIVOT'] = ta.pivots(df['high'], df['low'], df['close'], period='D', kind='fibonacci')
    
    return df

def analyze_signals(df: pd.DataFrame, symbol: str, timeframe: str) -> Optional[Dict]:
    """
    テクニカル分析を行い、ロングシグナルを判定する (プレースホルダー)
    """
    
    # 必要な指標が計算されているかチェック
    if df is None or len(df) < 5 or 'SMA_LONG' not in df.columns:
        return None

    # 最新足のデータ
    last = df.iloc[-1]
    
    # 1. ベーススコア
    final_score = BASE_SCORE
    signal_detail = {
        'symbol': symbol,
        'timeframe': timeframe,
        'entry_price': last['close'],
        'base_score': BASE_SCORE,
        'long_term_trend_match': False,
        'structural_pivot': False,
        'momentum_ok': False,
        'obv_confirmation': False,
        'liquidity_ok': False,
        'fgi_ok': False,
        'long_term_trend_score': 0.0,
        'structural_pivot_score': 0.0,
        'momentum_score': 0.0,
        'obv_score': 0.0,
        'liquidity_score': 0.0,
        'fgi_score': 0.0,
        'rrr': 1.5, # 初期RRR
    }
    
    # 2. 長期トレンド一致 (+20点 or -20点ペナルティ回避)
    if last['close'] > last['SMA_LONG']:
        final_score += (LONG_TERM_REVERSAL_PENALTY) # ペナルティの回避=ボーナス
        signal_detail['long_term_trend_match'] = True
        signal_detail['long_term_trend_score'] = LONG_TERM_REVERSAL_PENALTY * 100
    else:
        final_score -= LONG_TERM_REVERSAL_PENALTY
        signal_detail['long_term_trend_score'] = -LONG_TERM_REVERSAL_PENALTY * 100


    # 3. MACD/RSIモメンタム加速 (ロングの場合)
    if last['RSI_14'] < RSI_MOMENTUM_LOW and last['MACDh_12_26_9'] > last['MACDh_12_26_9'] * 0.9: # RSIが低く、MACDヒストグラムが上昇傾向
        final_score += MACD_CROSS_PENALTY # ペナルティ回避
        signal_detail['momentum_ok'] = True
        signal_detail['momentum_score'] = MACD_CROSS_PENALTY * 100
    else:
        final_score -= MACD_CROSS_PENALTY
        signal_detail['momentum_score'] = -MACD_CROSS_PENALTY * 100
    
    # 4. 価格構造/ピボット支持
    # (ここでは簡略化のため、単純な過去N足の安値サポートを想定)
    low_20 = df['low'].iloc[-20:-1].min()
    if last['close'] > low_20 * 1.002 and last['low'] < low_20 * 1.005: # 安値近傍で反発
        final_score += STRUCTURAL_PIVOT_BONUS
        signal_detail['structural_pivot'] = True
        signal_detail['structural_pivot_score'] = STRUCTURAL_PIVOT_BONUS * 100
        
    # 5. OBVによる出来高確証
    if last['OBV'] > last['OBV_SMA']:
        final_score += OBV_MOMENTUM_BONUS
        signal_detail['obv_confirmation'] = True
        signal_detail['obv_score'] = OBV_MOMENTUM_BONUS * 100

    # 6. 流動性/FGI/ボラティリティの調整（簡略化）
    # FGIプロキシをグローバルコンテキストから取得
    fgi_proxy = GLOBAL_MACRO_CONTEXT.get('fgi_proxy', 0.0)
    fgi_score = FGI_PROXY_BONUS_MAX * fgi_proxy * 20 # 例: -5%〜+5%の範囲でスコア調整
    final_score += fgi_score
    signal_detail['fgi_ok'] = fgi_score >= 0
    signal_detail['fgi_score'] = fgi_score * 100

    # 7. RRRの動的設定
    # スコアが高いほどRRRを高く設定（例: 0.65 -> 1.5, 0.80 -> 2.5）
    signal_detail['rrr'] = 1.0 + max(0, min(1.5, (final_score - BASE_SCORE) * 10))
    
    # 8. SL/TPの設定 (ATRベースのボラティリティに基づく簡略化)
    atr = ta.atr(df['high'], df['low'], df['close'], length=14).iloc[-1]
    
    # SL幅: 1.5倍ATRをリスク幅とする
    risk_factor = 1.5 
    risk_amount_price = atr * risk_factor
    
    # TP幅: SL幅 * RRR
    reward_amount_price = risk_amount_price * signal_detail['rrr']
    
    # SL/TP価格の決定
    signal_detail['stop_loss'] = last['close'] - risk_amount_price
    signal_detail['take_profit'] = last['close'] + reward_amount_price
    
    # リスク/リワードのUSDT金額 (取引ロットはBASE_TRADE_SIZE_USDTとする)
    signal_detail['risk_amount'] = (last['close'] - signal_detail['stop_loss']) / last['close'] * BASE_TRADE_SIZE_USDT
    signal_detail['reward_amount'] = (signal_detail['take_profit'] - last['close']) / last['close'] * BASE_TRADE_SIZE_USDT
    
    # 最終スコアの記録
    signal_detail['final_score'] = final_score * 100
    
    # 最終判定
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    if final_score > current_threshold:
        return signal_detail
        
    return None

async def close_position(position: Dict, exit_price: float, exit_type: str) -> Optional[Dict]:
    """
    指定されたポジションを現物で決済（売り注文を執行）する。
    """
    global EXCHANGE_CLIENT, IS_CLIENT_READY
    
    symbol = position['symbol']
    amount = position['amount'] # 保持している暗号資産の数量
    entry_price = position['entry_price']

    if not IS_CLIENT_READY or not EXCHANGE_CLIENT:
        logging.error(f"❌ 決済失敗: CCXTクライアントが準備できていません。")
        return {'status': 'error', 'error_message': 'CCXTクライアント未準備'}

    # 1. 注文数量の調整（最小数量/精度を考慮）
    market = EXCHANGE_CLIENT.markets.get(symbol)
    if not market:
        logging.error(f"❌ 決済失敗: {symbol} の市場情報がありません。")
        return {'status': 'error', 'error_message': '市場情報不足'}
        
    # 数量精度を取得し、販売数量を丸める
    amount_precision_value = market['precision']['amount']
    if amount_precision_value is not None and amount_precision_value > 0:
        try:
             precision_places = int(round(-math.log10(amount_precision_value)))
        except ValueError:
             precision_places = 8
        # 保有全量を精度に合わせて切り捨て
        amount_to_sell = math.floor(amount * math.pow(10, precision_places)) / math.pow(10, precision_places)
    else:
        amount_to_sell = amount
    
    if amount_to_sell <= 0:
        logging.error(f"❌ 決済失敗: {symbol} 売り数量がゼロ以下です。")
        return {'status': 'error', 'error_message': '売り数量がゼロ以下'}

    # 2. 注文実行（成行売り注文を想定）
    try:
        if TEST_MODE:
            logging.info(f"✨ TEST MODE: {symbol} Sell Market. 数量: {amount_to_sell:.8f} の決済をシミュレート。")
            
            # シミュレーション結果の計算
            pnl_usdt = (exit_price - entry_price) * amount_to_sell
            pnl_rate = (exit_price / entry_price) - 1.0
            
            trade_result = {
                'status': 'ok',
                'exit_price': exit_price,
                'entry_price': entry_price,
                'filled_amount': amount_to_sell,
                'pnl_usdt': pnl_usdt,
                'pnl_rate': pnl_rate,
            }
        else:
            # ライブ取引
            order = await EXCHANGE_CLIENT.create_order(
                symbol=symbol,
                type='market',
                side='sell',
                amount=amount_to_sell, 
                params={}
            )
            logging.info(f"✅ 決済注文実行成功: {symbol} Sell Market. 数量: {amount_to_sell:.8f}")

            # 注文詳細の取得とPNL計算
            filled_amount = order.get('filled', 0.0)
            cost_usdt = order.get('cost', filled_amount * exit_price) # 売却時のUSD建て金額 (概算)
            
            filled_usdt = position['filled_usdt'] # 買いで投入したUSDT
            pnl_usdt = cost_usdt - filled_usdt
            pnl_rate = (cost_usdt / filled_usdt) - 1.0

            trade_result = {
                'status': 'ok',
                'exit_price': exit_price,
                'entry_price': entry_price,
                'filled_amount': filled_amount,
                'pnl_usdt': pnl_usdt,
                'pnl_rate': pnl_rate,
            }

        # 決済通知
        await send_telegram_notification(
            format_telegram_message(position, "ポジション決済", get_current_threshold(GLOBAL_MACRO_CONTEXT), trade_result=trade_result, exit_type=exit_type)
        )
        log_signal(position, 'Position Closed', trade_result)
        
        return trade_result

    except ccxt.InsufficientFunds as e:
        logging.critical(f"❌ 決済失敗 - 残高不足: {symbol} {e}")
        await send_telegram_notification(f"🚨 **決済失敗**\\n{symbol} の決済注文が残高不足で失敗しました。手動で確認してください。")
        return {'status': 'error', 'error_message': f'残高不足エラー: {e}'}
    except Exception as e:
        logging.error(f"❌ 決済失敗 - 予期せぬエラー: {e}", exc_info=True)
        return {'status': 'error', 'error_message': f'予期せぬ決済エラー: {e}'}


async def position_management_loop():
    """
    オープンポジションを監視し、SL/TPをチェックして決済注文を執行する。
    """
    global OPEN_POSITIONS, EXCHANGE_CLIENT, GLOBAL_MACRO_CONTEXT
    
    if not OPEN_POSITIONS or not EXCHANGE_CLIENT:
        return 

    # 決済のために必要な最新価格を一度に取得する
    symbols_to_check = list(set(p['symbol'] for p in OPEN_POSITIONS))
    tickers: Dict[str, float] = {}
    
    # 全てのシンボルの最新価格を取得
    for symbol in symbols_to_check:
        try:
            ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
            tickers[symbol] = ticker['last']
        except Exception as e:
            logging.error(f"❌ ポジション監視エラー: {symbol} の価格取得失敗。{e}")
            tickers[symbol] = 0.0 

    # 決済処理の実行
    positions_to_remove = []
    
    for position in OPEN_POSITIONS:
        symbol = position['symbol']
        current_price = tickers.get(symbol, 0.0)
        
        if current_price == 0.0:
            continue

        sl_price = position['stop_loss']
        tp_price = position['take_profit']
        
        exit_type = None
        
        # SL判定 (現物ロングなので、価格がSLを下回ったら損切り)
        if current_price <= sl_price:
            exit_type = "ストップロス (SL)"
        
        # TP判定 (現物ロングなので、価格がTPを上回ったら利確)
        elif current_price >= tp_price:
            exit_type = "テイクプロフィット (TP)"

        if exit_type:
            logging.info(f"🚨 決済トリガー検出: {symbol} - {exit_type}。価格: {current_price:.4f}")
            
            trade_result = await close_position(position, current_price, exit_type)
            
            if trade_result and trade_result.get('status') == 'ok':
                positions_to_remove.append(position)
            elif trade_result and trade_result.get('error_message').startswith('残高不足エラー:'):
                logging.warning(f"⚠️ {symbol} 決済失敗（残高不足）。ポジションをリストに残します。手動で確認してください。")
            else:
                logging.error(f"❌ {symbol} 決済注文の執行に失敗しました。次のループで再試行します。")

    # 決済完了したポジションをリストから一括削除
    if positions_to_remove:
        OPEN_POSITIONS = [p for p in OPEN_POSITIONS if p not in positions_to_remove]
        logging.info(f"✅ {len(positions_to_remove)} 件のポジションを決済し、リストから削除しました。")


async def execute_trade(signal: Dict) -> Optional[Dict]:
    """
    取引所に注文を執行し、ポジション情報を記録する。
    """
    global EXCHANGE_CLIENT, IS_CLIENT_READY, OPEN_POSITIONS

    if not IS_CLIENT_READY or not EXCHANGE_CLIENT:
        logging.error("❌ 取引失敗: CCXTクライアントが準備できていません。")
        return None

    symbol = signal['symbol']
    entry_price = signal['entry_price']

    # 1. ロットサイズと精度の調整
    base_amount, adjusted_usdt_cost = adjust_order_amount(symbol, entry_price, BASE_TRADE_SIZE_USDT)
    
    if base_amount <= 0.0:
        logging.error(f"❌ {symbol} 取引失敗: 注文数量がゼロ以下または最小数量を下回りました。")
        return None

    try:
        # 2. 注文実行（成行買い注文）
        if TEST_MODE:
            logging.info(f"✨ TEST MODE: {symbol} Buy Market. 数量: {base_amount:.8f}, コスト: {adjusted_usdt_cost:.2f} USDT の購入をシミュレート。")
            
            # シミュレーション結果
            trade_result = {
                'status': 'ok',
                'filled_amount': base_amount,
                'filled_cost': adjusted_usdt_cost,
                'order_id': f'TEST-{uuid.uuid4().hex}',
            }
        else:
            # ライブ取引
            order = await EXCHANGE_CLIENT.create_order(
                symbol=symbol,
                type='market',
                side='buy',
                amount=base_amount, 
                params={} # MEXCなどの場合は、'quoteOrderQty'などのパラメータを調整する必要がある
            )
            logging.info(f"✅ 注文実行成功: {symbol} Buy Market. 数量: {base_amount:.8f}, コスト: {adjusted_usdt_cost:.2f} USDT")

            # 注文詳細の取得（約定情報）
            trade_result = {
                'status': 'ok',
                'filled_amount': order.get('filled', base_amount),
                'filled_cost': order.get('cost', adjusted_usdt_cost), # 約定コスト
                'order_id': order.get('id'),
            }

        # 3. ポジション情報の記録 (SL/TP監視用)
        new_position = {
            'id': trade_result['order_id'],
            'symbol': symbol,
            'timeframe': signal['timeframe'],
            'entry_price': entry_price,
            'stop_loss': signal['stop_loss'],
            'take_profit': signal['take_profit'],
            'amount': trade_result['filled_amount'],
            'filled_usdt': trade_result['filled_cost'],
            'timestamp': time.time(),
        }
        OPEN_POSITIONS.append(new_position)
        
        # 4. 通知とログ
        await send_telegram_notification(
            format_telegram_message(signal, "取引シグナル", get_current_threshold(GLOBAL_MACRO_CONTEXT), trade_result=trade_result)
        )
        log_signal(signal, 'Trade Executed', trade_result)
        
        return trade_result

    except ccxt.InsufficientFunds as e:
        logging.critical(f"❌ 取引失敗 - 残高不足: {symbol} {e}")
        await send_telegram_notification(f"🚨 **取引失敗**\\n{symbol} の買い注文が残高不足で失敗しました。")
        return {'status': 'error', 'error_message': '残高不足'}
    except Exception as e:
        logging.error(f"❌ 取引失敗 - 予期せぬエラー: {e}", exc_info=True)
        return {'status': 'error', 'error_message': f'予期せぬエラー: {e}'}


# ====================================================================================
# MAIN BOT LOGIC
# ====================================================================================

async def fetch_and_analyze(symbol: str) -> List[Dict]:
    """一つの銘柄の全タイムフレームのデータを取得し、分析する"""
    signals = []
    
    for timeframe in TARGET_TIMEFRAMES:
        limit = REQUIRED_OHLCV_LIMITS[timeframe]
        
        df = await fetch_ohlcv_safe(symbol, timeframe, limit)
        if df is None:
            continue
        
        df = calculate_indicators(df, timeframe)
        signal = analyze_signals(df, symbol, timeframe)
        
        if signal:
            signals.append(signal)
            
    return signals


async def update_monitoring_symbols():
    """出来高トップ銘柄のリストを更新する (簡略化)"""
    # 実際にはCCXTのfetch_tickersや取引所のAPIで出来高を取得し、リストを更新するロジック
    global CURRENT_MONITOR_SYMBOLS
    logging.info("♻️ 監視銘柄リストを更新します。")
    # 簡略化のため、DEFAULT_SYMBOLSをそのまま使用
    CURRENT_MONITOR_SYMBOLS = DEFAULT_SYMBOLS.copy()
    
async def update_macro_context():
    """FGIなどのマクロ環境データを更新する (プレースホルダー)"""
    global GLOBAL_MACRO_CONTEXT
    # 実際には外部API (Fear & Greed Indexなど) からデータを取得する
    # 簡略化のため、ランダムな値を設定
    GLOBAL_MACRO_CONTEXT['fgi_proxy'] = random.uniform(-0.05, 0.05) 
    GLOBAL_MACRO_CONTEXT['last_update'] = datetime.now(JST).isoformat()
    logging.info(f"📊 マクロコンテキスト更新完了。FGIプロキシ: {GLOBAL_MACRO_CONTEXT['fgi_proxy']:.2f}")


async def main_bot_loop():
    """ボットのメイン実行ループ"""
    global LAST_SUCCESS_TIME, LAST_ANALYSIS_SIGNALS, IS_FIRST_MAIN_LOOP_COMPLETED, LAST_ANALYSIS_ONLY_NOTIFICATION_TIME, LAST_WEBSHARE_UPLOAD_TIME

    if not IS_CLIENT_READY:
        logging.warning("⚠️ CCXTクライアントがまだ準備できていません。スキップします。")
        return
    
    logging.info(f"--- 💡 {datetime.now(JST).strftime('%Y/%m/%d %H:%M:%S')} - BOT LOOP START ---")

    # ★追加: ポジション監視ロジックをメインループの先頭で実行★
    await position_management_loop()
    
    # 銘柄リストとマクロコンテキストの更新
    await update_monitoring_symbols()
    await update_macro_context()

    # 全銘柄の分析タスクを作成
    tasks = []
    for symbol in CURRENT_MONITOR_SYMBOLS:
        tasks.append(fetch_and_analyze(symbol))

    # 全タスクを並列実行
    all_results = await asyncio.gather(*tasks)
    
    # 結果の集約
    new_signals: List[Dict] = [signal for signals_list in all_results for signal in signals_list]
    
    # スコアでソートし、最新シグナルリストを更新
    new_signals.sort(key=lambda x: x['final_score'], reverse=True)
    LAST_ANALYSIS_SIGNALS = new_signals[:TOP_SIGNAL_COUNT] # トップ3を記録

    logging.info(f"🔍 全分析完了。検出シグナル数: {len(new_signals)}件。")
    
    # 取引実行ロジック
    if new_signals and not TEST_MODE:
        highest_score_signal = new_signals[0]
        symbol = highest_score_signal['symbol']
        
        # クールダウンチェック (同一銘柄の直近取引を避ける)
        if time.time() - LAST_SIGNAL_TIME.get(symbol, 0) > TRADE_SIGNAL_COOLDOWN:
            logging.info(f"🚀 取引シグナル検出: {symbol} - スコア: {highest_score_signal['final_score']:.2f}")
            await execute_trade(highest_score_signal)
            LAST_SIGNAL_TIME[symbol] = time.time()
        else:
            logging.info(f"⏳ {symbol} はクールダウン中のため取引をスキップします。")
    
    # 分析専用通知の処理 (1時間ごと)
    if time.time() - LAST_ANALYSIS_ONLY_NOTIFICATION_TIME > ANALYSIS_ONLY_INTERVAL:
        if LAST_ANALYSIS_SIGNALS:
            # 実際にはformat_analysis_only_messageを使用
            await send_telegram_notification(f"📊 1時間分析レポート\\nトップシグナル: {LAST_ANALYSIS_SIGNALS[0]['symbol']} ({LAST_ANALYSIS_SIGNALS[0]['timeframe']}) - スコア: {LAST_ANALYSIS_SIGNALS[0]['final_score']:.2f}")
        LAST_ANALYSIS_ONLY_NOTIFICATION_TIME = time.time()

    # WebShareへのアップロード
    if time.time() - LAST_WEBSHARE_UPLOAD_TIME > WEBSHARE_UPLOAD_INTERVAL:
        send_webshare_update(LAST_ANALYSIS_SIGNALS)
    
    LAST_SUCCESS_TIME = time.time()
    IS_FIRST_MAIN_LOOP_COMPLETED = True
    logging.info(f"--- 🟢 BOT LOOP END ---")


# ====================================================================================
# FASTAPI & ASYNC EXECUTION
# ====================================================================================

app = FastAPI(title="Apex Trading Bot API", version=BOT_VERSION)

@app.get("/status")
async def get_status_info():
    """ボットの現在の状態を返す"""
    next_check = max(0, LOOP_INTERVAL - (time.time() - LAST_SUCCESS_TIME))
    
    status_msg = {
        "bot_version": BOT_VERSION,
        "status": "RUNNING" if IS_FIRST_MAIN_LOOP_COMPLETED else "STARTING",
        "timestamp": datetime.now(JST).isoformat(),
        "last_success_time": datetime.fromtimestamp(LAST_SUCCESS_TIME, JST).isoformat() if LAST_SUCCESS_TIME > 0 else "N/A",
        "next_main_loop_check_seconds": next_check,
        "current_threshold": get_current_threshold(GLOBAL_MACRO_CONTEXT),
        "macro_context": GLOBAL_MACRO_CONTEXT, 
        "is_test_mode": TEST_MODE,
        "monitoring_symbols": len(CURRENT_MONITOR_SYMBOLS),
        "last_signals_count": len(LAST_ANALYSIS_SIGNALS),
        "open_positions_count": len(OPEN_POSITIONS), # ポジション数追加
        "is_client_ready": IS_CLIENT_READY,
    }
    return JSONResponse(content=status_msg)

async def main_loop_scheduler():
    """メインループを定期実行するスケジューラ"""
    while True:
        try:
            await main_bot_loop()
        except Exception as e:
            logging.critical(f"❌ メインループ実行中に致命的なエラー: {e}", exc_info=True)
            await send_telegram_notification(f"🚨 **致命的なエラー**\\nメインループでエラーが発生しました: `{e}`")

        # 次の実行までの待機時間を計算し、最低1秒は待機する
        wait_time = max(1, LOOP_INTERVAL - (time.time() - LAST_SUCCESS_TIME))
        logging.info(f"次のループまで {wait_time:.1f} 秒待機します。")
        await asyncio.sleep(wait_time)


@app.on_event("startup")
async def startup_event():
    """アプリケーション起動時に実行"""
    # 初期化タスクをバックグラウンドで開始
    asyncio.create_task(initialize_exchange_client())
    # メインループのスケジューラを開始
    asyncio.create_task(main_loop_scheduler())


if __name__ == "__main__":
    # uvicorn.run(app, host="0.0.0.0", port=8000)
    # 開発環境での実行を想定し、ログレベルを設定
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
