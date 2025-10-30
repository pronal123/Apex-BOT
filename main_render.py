# ====================================================================================
# Apex BOT v19.0.29 - High-Freq/TP/SL/M1M5 Added (Patch 40 - Live FGI)
#
# 実践稼働向け改良版: ダミーロジックを排除し、RSI, MACD, BB, EMAに基づく動的なテクニカル分析を完全実装。
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

# 💡 【ログ確認対応】ロギング設定を明示的に定義
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - (%(funcName)s) - %(message)s' 
)

# ====================================================================================
# CONFIG & CONSTANTS
# ====================================================================================

# --- API Keys & Credentials ---
API_KEY = os.getenv("BINANCE_API_KEY", "YOUR_API_KEY")
SECRET_KEY = os.getenv("BINANCE_SECRET_KEY", "YOUR_SECRET_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID")
WEB_SHARE_URL = os.getenv("WEB_SHARE_URL", "https://your.webshare.server/log")

# --- System & Loop Settings ---
LOOP_INTERVAL = 60 * 1           # メイン分析ループの実行間隔 (60秒ごと)
MONITOR_INTERVAL = 10            # SL/TP監視ループの実行間隔 (10秒ごと)
TARGET_TIMEFRAMES = ['1m', '5m', '15m', '1h', '4h'] # 分析対象のタイムフレーム
BASE_TRADE_SIZE_USDT = 100.0     # デフォルトの取引サイズ (USDT換算)
TRADE_SIGNAL_COOLDOWN = 60 * 60 * 2 # 同一銘柄の取引クールダウン (2時間)

# --- Trading Logic Settings ---
BASE_SIGNAL_SCORE = 0.35         # 基本スコア
SIGNAL_THRESHOLD_NORMAL = 0.85   # 通常時の取引実行に必要な最低スコア (FGIで動的変更される)

# 【動的スコアリングのためのボーナス値】
TA_BONUS_LONG_TREND = 0.20       # 長期トレンド (EMA200) 確認ボーナス
TA_BONUS_SHORT_TREND = 0.10      # 短期モメンタム (EMA50) 確認ボーナス
TA_BONUS_RSI_REVERSION = 0.15    # RSI < 40 反転ボーナス
TA_BONUS_MACD_MOMENTUM = 0.15    # MACD強気クロスボーナス
TA_BONUS_BB_REVERSION = 0.10     # ボリンジャーバンド下限タッチボーナス
TA_BONUS_VOLUME_CONFIRM = 0.10   # OBV/出来高によるトレンド確証ボーナス
TA_BONUS_LIQUIDITY = 0.05        # 流動性ボーナス (ハードコードで許容)

# FGI (恐怖・貪欲指数) 設定
FGI_PROXY_BONUS_MAX = 0.05       # FGIによる最大ボーナス
FGI_PROXY_PENALTY_MAX = -0.05    # FGIによる最大ペナルティ
FGI_SLUMP_THRESHOLD = -0.02      # これを下回ると閾値が0.90に厳格化
FGI_ACTIVE_THRESHOLD = 0.02      # これを上回ると閾値が0.75に緩和

# SL/TP設定
BASE_RISK_RATIO = 0.015          # ベースとなる最大リスク幅 (1.5%)
MIN_RISK_RATIO = 0.010           # 最小リスク幅 (1.0%)
MAX_RISK_RATIO_REDUCTION = 0.005 # 構造的ボーナスで削減できる最大幅
RRR_MIN = 1.0                    # 最小リスクリワード比率
RRR_MAX = 3.0                    # 最大リスクリワード比率

# 【削除済】為替マクロ影響は完全に削除
# FOREX_BONUS_MAX = 0.0

# ====================================================================================
# GLOBAL STATE
# ====================================================================================
EXCHANGE_CLIENT: Optional[ccxt_async.Exchange] = None
# { 'symbol': {'entry_price': float, 'sl_price': float, 'tp_price': float, 'amount': float, 'timestamp': datetime, 'order_id': str, 'status': str}}
OPEN_POSITIONS: Dict[str, Dict[str, Any]] = {} 
# { 'symbol': datetime } - クールダウン中の銘柄
COOLDOWN_SYMBOLS: Dict[str, datetime] = {}
LAST_SUCCESS_TIME: float = time.time()
# FGIなどのマクロ情報
GLOBAL_MACRO_CONTEXT: Dict[str, Any] = {'fgi_proxy': 0.0, 'fgi_value': 50, 'fgi_timestamp': None}

# FastAPIアプリケーションの初期化
app = FastAPI(title="Apex Bot Live Monitor")

# ====================================================================================
# HELPER FUNCTIONS - 通信・ユーティリティ
# ====================================================================================

async def send_telegram_notification(message: str):
    """Telegramにメッセージを送信します。"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("Telegram設定が不足しています。通知をスキップします。")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'Markdown'
    }
    
    try:
        response = requests.post(url, data=payload, timeout=5)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logging.error(f"Telegram通知エラー: {e}")

async def send_webshare_log(log_data: Dict[str, Any]):
    """WebShareサーバーにログデータを送信します。"""
    if not WEB_SHARE_URL:
        return
    
    try:
        response = requests.post(WEB_SHARE_URL, json=log_data, timeout=5)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logging.error(f"WebShareログ送信エラー: {e}")

def adjust_order_amount(target_usdt: float, price: float, min_amount: float, precision: int) -> float:
    """
    取引所の制限に合わせて注文数量を調整します。
    """
    if price <= 0: return 0.0
    
    # 1. USDTベースの目標数量を計算
    amount_float = target_usdt / price
    
    # 2. 数量の精度を適用
    multiplier = 10 ** precision
    amount_adjusted = math.floor(amount_float * multiplier) / multiplier
    
    # 3. 最小注文数量をチェック
    if amount_adjusted < min_amount:
        # logging.warning(f"調整後の数量 {amount_adjusted} が最小数量 {min_amount} を下回りました。最小数量を使用します。")
        return min_amount
        
    return amount_adjusted

def get_exchange_info(symbol: str) -> Optional[Dict[str, Any]]:
    """取引所のシンボル情報を取得します。"""
    global EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT or not EXCHANGE_CLIENT.markets:
        logging.error("取引所クライアントまたはマーケット情報が未初期化です。")
        return None
        
    market = EXCHANGE_CLIENT.markets.get(symbol)
    if not market:
        logging.error(f"シンボル情報が見つかりません: {symbol}")
        return None
        
    # 必要な情報を抽出
    limits = market.get('limits', {})
    amount_limits = limits.get('amount', {})
    
    return {
        'min_amount': amount_limits.get('min', 0.0),
        'precision': market.get('precision', {}).get('amount', 8),
        # 'price_precision': market.get('precision', {}).get('price', 8),
    }

# ====================================================================================
# MACRO & THRESHOLD LOGIC
# ====================================================================================

async def fetch_fgi_data():
    """外部APIからFGI（恐怖・貪欲指数）をライブ取得し、-1.0から1.0に正規化します。"""
    url = "https://api.alternative.me/fng/?limit=1"
    
    try:
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        data = response.json().get('data', [])
        
        if data:
            fgi_value = int(data[0]['value']) # 0-100の値
            fgi_proxy = (fgi_value - 50) / 50.0 # -1.0から1.0に正規化

            # グローバルコンテキストを更新
            GLOBAL_MACRO_CONTEXT.update({
                'fgi_proxy': fgi_proxy,
                'fgi_value': fgi_value,
                'fgi_timestamp': data[0]['timestamp']
            })
            logging.info(f"✅ FGIライブデータ更新: Value={fgi_value}, Proxy={fgi_proxy:.2f}")
    
    except requests.exceptions.RequestException as e:
        logging.error(f"FGIデータ取得エラー: {e}")
        # 取得失敗時は値をリセットまたは維持 (ここでは維持)
        pass

def get_fgi_proxy_bonus(fgi_proxy: float) -> float:
    """FGIプロキシ値に基づき、スコアに加算するボーナス/ペナルティを計算します。"""
    # 恐怖 (> 0.0) -> ロングを推奨 (ボーナス)
    # 貪欲 (< 0.0) -> ロングを慎重に (ペナルティ)
    
    if fgi_proxy < 0: # 市場が貪欲 (高値圏)
        # 貪欲になるほどペナルティ
        bonus = max(FGI_PROXY_PENALTY_MAX, fgi_proxy * 0.1) # -0.05まで
    elif fgi_proxy > 0: # 市場が恐怖 (安値圏)
        # 恐怖になるほどボーナス
        bonus = min(FGI_PROXY_BONUS_MAX, fgi_proxy * 0.1) # +0.05まで
    else:
        bonus = 0.0
        
    return bonus

def get_dynamic_threshold(fgi_proxy: float) -> float:
    """FGIプロキシ値に基づき、取引実行に必要な最低スコアを動的に決定します。"""
    
    if fgi_proxy < FGI_SLUMP_THRESHOLD: # 極度の恐怖または低迷
        return 0.90 # 厳格化
    elif fgi_proxy > FGI_ACTIVE_THRESHOLD: # 活発または極度の貪欲
        return 0.75 # 緩和
    else:
        return SIGNAL_THRESHOLD_NORMAL # 通常 (0.85)

# ====================================================================================
# DATA & INDICATOR ANALYSIS
# ====================================================================================

async def fetch_ohlcv_safe(symbol: str, exchange: ccxt_async.Exchange, timeframe: str, limit: int) -> pd.DataFrame:
    """OHLCVデータを安全に取得します。"""
    try:
        ohlcv = await exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
        return df
    except Exception as e:
        logging.error(f"OHLCVデータ取得エラー ({symbol}, {timeframe}): {e}")
        return pd.DataFrame()

def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """OHLCVデータにテクニカルインジケーターを**完全**に追加します。"""
    if df.empty or len(df) < 200:
        return df.iloc[0:0] # データ不足

    # 1. トレンド/移動平均
    df['EMA50'] = ta.ema(df['close'], length=50)
    df['EMA200'] = ta.ema(df['close'], length=200)
    df['SMA200'] = ta.sma(df['close'], length=200)

    # 2. モメンタム/オシレーター
    # RSI (Relative Strength Index)
    df['RSI'] = ta.rsi(df['close'], length=14)
    # MACD (Moving Average Convergence Divergence)
    macd_results = ta.macd(df['close'], fast=12, slow=26, signal=9)
    df = pd.concat([df, macd_results], axis=1) # MACD_12_26_9, MACDs_12_26_9, MACDh_12_26_9

    # 3. ボラティリティ/バンド
    # Bollinger Bands
    bbands_results = ta.bbands(df['close'], length=20, std=2)
    df = pd.concat([df, bbands_results], axis=1) # BBL_20_2.0, BBU_20_2.0

    # 4. 出来高/出来高オシレーター
    # OBV (On-Balance Volume)
    df['OBV'] = ta.obv(df['close'], df['volume'])
    df['OBV_EMA20'] = ta.ema(df['OBV'], length=20)

    # NaNを処理し、最新データのみを使用
    df = df.dropna().reset_index(drop=True)

    return df

async def analyze_signals(symbol: str, exchange: ccxt_async.Exchange, timeframe: str) -> Tuple[float, Dict[str, Any]]:
    """
    指定されたシンボルと時間足でテクニカル分析を行い、取引シグナルスコアを動的に算出します。
    """
    df = await fetch_ohlcv_safe(symbol, exchange, timeframe, limit=300)
    if df.empty or len(df) < 200:
        return 0.0, {'status': 'Insufficient Data'}

    df = calculate_indicators(df)
    if df.empty:
        return 0.0, {'status': 'Indicators Failed'}

    last = df.iloc[-1]
    close = last['close']
    
    # 1. ベーススコア
    score = BASE_SIGNAL_SCORE # 0.35

    # 2. マクロ感情のボーナス/ペナルティ (FGI)
    fgi_proxy = GLOBAL_MACRO_CONTEXT.get('fgi_proxy', 0.0)
    sentiment_fgi_proxy_bonus = get_fgi_proxy_bonus(fgi_proxy)
    score += sentiment_fgi_proxy_bonus

    # =============================================================
    # 3. 【動的スコアリング】テクニカルボーナスの計算 (ダミー排除)
    # =============================================================
    
    # --- A. トレンドフィルター (長期/短期) ---
    is_long_term_trend_up = (close > last['EMA200']) and (close > last['SMA200'])
    is_short_term_momentum_up = (close > last['EMA50']) and (last['EMA50'] > last['EMA200'])

    bonus_long_trend = TA_BONUS_LONG_TREND if is_long_term_trend_up else 0.0
    bonus_short_momentum = TA_BONUS_SHORT_TREND if is_short_term_momentum_up else 0.0
    
    score += bonus_long_trend
    score += bonus_short_momentum

    # --- B. モメンタム/反転シグナル ---
    is_rsi_reversion_met = (last['RSI'] < 40.0) # RSI < 40 は売られすぎ
    is_macd_momentum_met = (
        last.get('MACD_12_26_9', -99) > last.get('MACDs_12_26_9', -99) # MACD線がシグナル線の上
        and last.get('MACDh_12_26_9', -99) > 0 # ヒストグラムが0より大
    )

    bonus_rsi_reversion = TA_BONUS_RSI_REVERSION if is_rsi_reversion_met else 0.0
    bonus_macd_momentum = TA_BONUS_MACD_MOMENTUM if is_macd_momentum_met else 0.0
    
    score += bonus_rsi_reversion
    score += bonus_macd_momentum

    # --- C. ボラティリティ/出来高 ---
    bbl = last.get('BBL_20_2.0', close * 0.9) # BBLの安全な取得
    is_bb_reversion_met = (close < bbl) or (close < bbl * 1.002) # BBLを下回るか、ごく近い場合
    is_volume_confirm_met = (last.get('OBV', 0) > last.get('OBV_EMA20', 0))

    bonus_bb_reversion = TA_BONUS_BB_REVERSION if is_bb_reversion_met else 0.0
    bonus_volume_confirm = TA_BONUS_VOLUME_CONFIRM if is_volume_confirm_met else 0.0

    score += bonus_bb_reversion
    score += bonus_volume_confirm

    # --- D. 流動性/構造 ---
    # 流動性ボーナスは固定値 (簡略化のため)
    bonus_liquidity = TA_BONUS_LIQUIDITY # 0.05
    score += bonus_liquidity
    
    # ピボット支持の有無 (TP/SLロジックで利用するため、ここではスコア加算しない)
    # 構造的な支持があるかをEMA200付近で判定 (簡略化)
    has_structural_support = (close * 0.995 < last['EMA200'] < close * 1.005) # 価格がEMA200に非常に近い

    # =============================================================
    # 4. SL/TPと最終スコアの計算
    # =============================================================
    
    # SL/TPの動的設定
    structural_pivot_bonus = MAX_RISK_RATIO_REDUCTION if has_structural_support else 0.0
    
    # SL価格の決定: ベースリスク率を構造的要因で削減
    risk_ratio = max(MIN_RISK_RATIO, BASE_RISK_RATIO - structural_pivot_bonus)
    sl_price = close * (1 - risk_ratio)
    
    # RRRの決定: スコアが高いほどRRRを改善
    max_possible_score = BASE_SIGNAL_SCORE + FGI_PROXY_BONUS_MAX + sum([
        TA_BONUS_LONG_TREND, TA_BONUS_SHORT_TREND, TA_BONUS_RSI_REVERSION,
        TA_BONUS_MACD_MOMENTUM, TA_BONUS_BB_REVERSION, TA_BONUS_VOLUME_CONFIRM,
        TA_BONUS_LIQUIDITY
    ]) # 約 1.05
    
    # スコアに基づいてRRRを線形補間 (0.70で1.0, 1.05で3.0)
    score_normalized = (score - 0.70) / (max_possible_score - 0.70)
    dynamic_rr_ratio = min(RRR_MAX, max(RRR_MIN, RRR_MIN + (RRR_MAX - RRR_MIN) * score_normalized))
    
    # TP価格の決定: RRR * リスク幅
    reward_ratio = risk_ratio * dynamic_rr_ratio
    tp_price = close * (1 + reward_ratio)

    # 最終的なスコア
    final_score = score
    
    # テクニカルデータの詳細 (ブレークダウン)
    tech_data = {
        'status': 'Signal Calculated',
        'close_price': close,
        'timeframe': timeframe,
        'fgi_proxy': fgi_proxy,
        'sentiment_fgi_proxy_bonus': sentiment_fgi_proxy_bonus,
        
        # 動的TAボーナスのブレークダウン
        'long_trend_bonus': bonus_long_trend,
        'short_momentum_bonus': bonus_short_momentum,
        'rsi_reversion_bonus': bonus_rsi_reversion,
        'macd_momentum_bonus': bonus_macd_momentum,
        'bb_reversion_bonus': bonus_bb_reversion,
        'volume_confirm_bonus': bonus_volume_confirm,
        'liquidity_bonus': bonus_liquidity,

        # SL/TP情報
        'risk_ratio_used': risk_ratio,
        'dynamic_rr_ratio': dynamic_rr_ratio,
        'sl_price': sl_price,
        'tp_price': tp_price,
    }

    return final_score, tech_data

# ====================================================================================
# TRADE EXECUTION & MANAGEMENT
# ====================================================================================

async def execute_trade(symbol: str, price: float, tech_data: Dict[str, Any]):
    """取引を実行し、ポジションを記録します（現物成行買い）。"""
    global OPEN_POSITIONS
    
    if symbol in OPEN_POSITIONS:
        logging.warning(f"⚠️ {symbol}: すでにオープンポジションがあります。取引をスキップします。")
        return
        
    exchange_info = get_exchange_info(symbol)
    if not exchange_info:
        await send_telegram_notification(f"🚨 **取引失敗**\\n`{symbol}`: 取引所情報取得エラー。")
        return
        
    try:
        # 注文数量の計算と調整
        target_amount = adjust_order_amount(BASE_TRADE_SIZE_USDT, price, 
                                            exchange_info['min_amount'], exchange_info['precision'])
        
        if target_amount <= 0:
            await send_telegram_notification(f"🚨 **取引失敗**\\n`{symbol}`: 注文数量がゼロまたは最小数量以下です。")
            return
            
        logging.info(f"⏳ {symbol}: 成行買い注文実行: 数量={target_amount:.8f} @ {price:.8f}")
        
        # 注文実行 (現物成行買い)
        order = await EXCHANGE_CLIENT.create_market_buy_order(symbol, target_amount)

        entry_price = order['price'] or price # 注文価格が取得できればそちらを優先
        
        # ポジションの記録
        OPEN_POSITIONS[symbol] = {
            'entry_price': entry_price,
            'sl_price': tech_data['sl_price'],
            'tp_price': tech_data['tp_price'],
            'amount': target_amount,
            'timestamp': datetime.now(timezone.utc),
            'order_id': order['id'],
            'status': 'open',
            'tech_data': tech_data,
        }
        
        # クールダウンタイマー設定
        COOLDOWN_SYMBOLS[symbol] = datetime.now(timezone.utc)

        # 通知
        message = (
            f"🚀 **新規取引実行**\n"
            f"銘柄: `{symbol}` ({tech_data['timeframe']})\n"
            f"スコア: `{tech_data['final_score']:.4f}` (閾値: `{get_dynamic_threshold(fgi_proxy):.2f}`)\n"
            f"エントリー: `{entry_price:.8f}`\n"
            f"SL/TP: `{tech_data['sl_price']:.8f}` / `{tech_data['tp_price']:.8f}`\n"
            f"RRR: `{tech_data['dynamic_rr_ratio']:.2f}` (リスク: `{tech_data['risk_ratio_used']:.2%}`)\n"
            f"数量: `{target_amount:.4f}`"
        )
        await send_telegram_notification(message)

    except ccxt.InsufficientFunds as e:
        logging.error(f"❌ {symbol} 取引失敗: 資金不足。")
        await send_telegram_notification(f"🚨 **取引失敗**\\n`{symbol}`: 資金不足です。残高を確認してください。")
    except ccxt.InvalidOrder as e:
        logging.error(f"❌ {symbol} 取引失敗: 無効な注文パラメータ。{e}")
        await send_telegram_notification(f"🚨 **取引失敗**\\n`{symbol}`: 無効な注文パラメータ。")
    except Exception as e:
        logging.error(f"❌ {symbol} 取引実行エラー: {e}")
        await send_telegram_notification(f"🚨 **取引エラー**\\n`{symbol}`: `{e}`")

async def liquidate_position(symbol: str, position: Dict[str, Any], reason: str):
    """ポジションを決済します（現物成行売り）。"""
    global OPEN_POSITIONS
    
    if symbol not in OPEN_POSITIONS:
        logging.warning(f"⚠️ {symbol}: 決済対象のポジションが見つかりません。")
        return

    # 現物売却数量を決定
    amount_to_sell = position['amount']
    
    try:
        # 注文実行 (現物成行売り)
        logging.info(f"⏳ {symbol}: {reason}のため、成行売り注文実行: 数量={amount_to_sell:.8f}")
        order = await EXCHANGE_CLIENT.create_market_sell_order(symbol, amount_to_sell)
        
        exit_price = order['price'] or EXCHANGE_CLIENT.last_prices.get(symbol, position['entry_price'])
        entry_price = position['entry_price']
        
        # 損益計算 (USDTベース)
        pnl_ratio = (exit_price / entry_price) - 1.0
        pnl_usdt = (exit_price - entry_price) * amount_to_sell
        
        # ポジションをクローズ
        del OPEN_POSITIONS[symbol]

        # 通知
        message = (
            f"✅ **ポジション決済** ({reason})\n"
            f"銘柄: `{symbol}`\n"
            f"エントリー: `{entry_price:.8f}`\n"
            f"決済価格: `{exit_price:.8f}`\n"
            f"損益率: `{pnl_ratio:.2%}`\n"
            f"損益額: `{pnl_usdt:.2f} USDT`"
        )
        await send_telegram_notification(message)
        logging.info(f"✅ {symbol} 決済完了。損益率: {pnl_ratio:.2%}、理由: {reason}")
        
    except Exception as e:
        logging.error(f"❌ {symbol} 決済実行エラー: {e}")
        await send_telegram_notification(f"🚨 **決済エラー**\\n`{symbol}`: 決済中にエラーが発生しました: `{e}`")


async def position_management_loop_async():
    """オープンポジションのSL/TPを監視するループ (10秒ごと)。"""
    global OPEN_POSITIONS
    
    if not EXCHANGE_CLIENT or not OPEN_POSITIONS:
        return

    symbols_to_check = list(OPEN_POSITIONS.keys())
    if not symbols_to_check:
        return

    try:
        # 監視対象の最新価格を一括で取得
        tickers = await EXCHANGE_CLIENT.fetch_tickers(symbols_to_check)
    except Exception as e:
        logging.error(f"❌ ポジション監視: 最新価格の取得エラー: {e}")
        return

    # ポジションのチェックと決済
    for symbol, position in list(OPEN_POSITIONS.items()):
        ticker = tickers.get(symbol)
        if not ticker:
            logging.warning(f"⚠️ 監視中の {symbol} のティッカー情報が取得できませんでした。スキップします。")
            continue
            
        current_price = ticker.get('last')
        if current_price is None:
            continue

        sl_price = position['sl_price']
        tp_price = position['tp_price']

        if current_price <= sl_price:
            # SLトリガー
            await liquidate_position(symbol, position, "STOP LOSS")
        elif current_price >= tp_price:
            # TPトリガー
            await liquidate_position(symbol, position, "TAKE PROFIT")
        # else:
            # logging.info(f"{symbol}: SL/TP範囲内 ({sl_price:.4f} < {current_price:.4f} < {tp_price:.4f})")


# ====================================================================================
# MAIN BOT LOGIC
# ====================================================================================

async def main_bot_loop():
    """メインの分析と取引実行ループ (60秒ごと)。"""
    global LAST_SUCCESS_TIME, COOLDOWN_SYMBOLS

    # 1. FGIデータの更新
    await fetch_fgi_data()

    # 2. クールダウン解除処理
    now = datetime.now(timezone.utc)
    # クールダウンが終了した銘柄を削除
    COOLDOWN_SYMBOLS = {
        s: dt for s, dt in COOLDOWN_SYMBOLS.items()
        if (now - dt).total_seconds() < TRADE_SIGNAL_COOLDOWN
    }

    # 3. 取引候補の選定 (ここでは簡略化のため、BTC/USDTのみをターゲットとする)
    target_symbols = ['BTC/USDT'] 
    
    # 4. 分析の実行
    for symbol in target_symbols:
        # 既にオープンポジションがあるか、クールダウン中か
        if symbol in OPEN_POSITIONS:
            logging.info(f"⏭️ {symbol}: ポジションオープン中のためスキップ。")
            continue
        if symbol in COOLDOWN_SYMBOLS:
            logging.info(f"⏭️ {symbol}: クールダウン期間中のためスキップ。")
            continue

        # 複数のタイムフレームを並行して分析 (最も高いスコアを採用)
        best_score = 0.0
        best_tech_data: Optional[Dict[str, Any]] = None
        
        analysis_tasks = [analyze_signals(symbol, EXCHANGE_CLIENT, tf) for tf in TARGET_TIMEFRAMES]
        results = await asyncio.gather(*analysis_tasks)
        
        for score, tech_data in results:
            if score > best_score:
                best_score = score
                best_tech_data = tech_data

        # 5. シグナルの評価と実行
        if best_tech_data is not None:
            dynamic_threshold = get_dynamic_threshold(GLOBAL_MACRO_CONTEXT.get('fgi_proxy', 0.0))
            current_rr_ratio = best_tech_data.get('dynamic_rr_ratio', 0.0)

            if best_score >= dynamic_threshold and current_rr_ratio >= RRR_MIN:
                logging.warning(f"✅ 【シグナル検出】{symbol} ({best_tech_data['timeframe']}) - スコア: {best_score:.4f} (閾値: {dynamic_threshold:.2f})")
                
                # 取引実行
                await execute_trade(symbol, best_tech_data['close_price'], best_tech_data)
            else:
                logging.info(f"❌ {symbol}: スコア不足 ({best_score:.4f} / 閾値: {dynamic_threshold:.2f}) または RRR不足 ({current_rr_ratio:.2f})。")

    # 6. WebShareログの送信 (1時間ごと)
    if (time.time() - LAST_SUCCESS_TIME) > 60 * 60:
        await send_webshare_log({
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'open_positions': OPEN_POSITIONS,
            'cooldown_symbols': {s: dt.isoformat() for s, dt in COOLDOWN_SYMBOLS.items()},
            'macro_context': GLOBAL_MACRO_CONTEXT
        })
    
    LAST_SUCCESS_TIME = time.time()


# ====================================================================================
# SCHEDULER & APPLICATION SETUP
# ====================================================================================

async def initialize_exchange_client():
    """取引所クライアントを初期化し、マーケット情報をロードします。"""
    global EXCHANGE_CLIENT
    try:
        EXCHANGE_CLIENT = ccxt_async.binance({
            'apiKey': API_KEY,
            'secret': SECRET_KEY,
            'enableRateLimit': True,
        })
        logging.info("⏳ 取引所クライアントをロード中...")
        await EXCHANGE_CLIENT.load_markets()
        logging.info("✅ 取引所クライアントとマーケット情報のロードが完了しました。")
        await send_telegram_notification("🤖 **Apex BOT v19.0.29**\n現物取引モードで起動しました。")
        # 最初のFGIデータを取得
        await fetch_fgi_data()

    except Exception as e:
        logging.critical(f"❌ 取引所クライアント初期化エラー: {e}")
        sys.exit(1)


async def main_bot_scheduler():
    """メイン分析ループを定期実行するスケジューラ (60秒ごと)。"""
    while True:
        if EXCHANGE_CLIENT is not None:
            try:
                await main_bot_loop()
            except Exception as e:
                logging.critical(f"❌ メインループ実行中に致命的なエラー: {e}", exc_info=True)
                await send_telegram_notification(f"🚨 **致命的なエラー**\\nメインループでエラーが発生しました: `{e}`")

        # 待機時間を LOOP_INTERVAL (60秒) に基づいて計算
        wait_time = max(1, LOOP_INTERVAL - (time.time() - LAST_SUCCESS_TIME))
        logging.info(f"次のメインループまで {wait_time:.1f} 秒待機します。")
        await asyncio.sleep(wait_time)

async def position_monitor_scheduler():
    """TP/SL監視ループを定期実行するスケジューラ (10秒ごと)。"""
    while True:
        if EXCHANGE_CLIENT is not None:
            try:
                await position_management_loop_async()
            except Exception as e:
                logging.critical(f"❌ ポジション監視ループ実行中に致命的なエラー: {e}", exc_info=True)

        await asyncio.sleep(MONITOR_INTERVAL) # MONITOR_INTERVAL (10秒) ごとに実行


@app.on_event("startup")
async def startup_event():
    """アプリケーション起動時に実行 (タスク起動)"""
    # 初期化タスクをバックグラウンドで開始
    asyncio.create_task(initialize_exchange_client())
    # メインループのスケジューラをバックグラウンドで開始 (1分ごと)
    asyncio.create_task(main_bot_scheduler())
    # ポジション監視スケジューラをバックグラウンドで開始 (10秒ごと)
    asyncio.create_task(position_monitor_scheduler())


@app.on_event("shutdown")
async def shutdown_event():
    """アプリケーション終了時に実行"""
    if EXCHANGE_CLIENT:
        await EXCHANGE_CLIENT.close()
    await send_telegram_notification("🛑 **Apex BOT**\nシャットダウンしました。")


@app.get("/status")
async def get_status():
    """現在のボットの状態をJSONで返すエンドポイント"""
    return JSONResponse({
        'status': 'running',
        'last_success_time': datetime.fromtimestamp(LAST_SUCCESS_TIME).isoformat(),
        'open_positions_count': len(OPEN_POSITIONS),
        'open_positions': OPEN_POSITIONS,
        'cooldown_symbols': {s: dt.isoformat() for s, dt in COOLDOWN_SYMBOLS.items()},
        'macro_context': GLOBAL_MACRO_CONTEXT
    })


# 実行部分 (FastAPIの起動)
if __name__ == "__main__":
    # uvicornはFastAPIアプリケーションを実行するためのWSGIサーバー
    uvicorn.run(app, host="0.0.0.0", port=8000)
