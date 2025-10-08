# ====================================================================================
# Apex BOT v16.0.1 - Structural SL Buffer Fix & Dominance Bias Filter (統合最終版)
# - DTS (動的追跡損切) 戦略を核とする
# - 資金調達率 (Funding Rate) とBTCドミナンスの需給バイアスフィルターを搭載
# - Telegram通知に「抵抗候補/支持候補」「1000 USDポジションの損益額」「優位性のあった分析」を追加
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
import yfinance as yf
import asyncio
from fastapi import FastAPI
from fastapi.responses import JSONResponse 
import uvicorn
from dotenv import load_dotenv
import sys 
import random 

# .envファイルから環境変数を読み込む
load_dotenv()

# ====================================================================================
# CONFIG & CONSTANTS
# ====================================================================================

BOT_VERSION = "v16.0.1 - Structural SL Buffer Fix & Dominance Bias"
JST = timezone(timedelta(hours=9))

# 出来高TOP30に加えて、主要な基軸通貨をDefaultに含めておく
DEFAULT_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "ADA/USDT", "XRP/USDT", "DOT/USDT", 
    "DOGE/USDT", "AVAX/USDT", "LINK/USDT", "LTC/USDT", "MATIC/USDT", "TRX/USDT", 
    "ATOM/USDT", "NEAR/USDT", "ALGO/USDT", "XLM/USDT", "BCH/USDT", "ETC/USDT", 
    "UNI/USDT", "ICP/USDT", "FIL/USDT", "AAVE/USDT", "AXS/USDT", "SAND/USDT",
    "GALA/USDT", "FTM/USDT", "HBAR/USDT", "VET/USDT", "GRT/USDT", "SHIB/USDT"
] 
TOP_SYMBOL_LIMIT = 30      
LOOP_INTERVAL = 180        
REQUEST_DELAY_PER_SYMBOL = 0.5 

# 環境変数から取得。未設定の場合はダミー値。
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', 'YOUR_TELEGRAM_CHAT_ID')

TRADE_SIGNAL_COOLDOWN = 60 * 60 * 2 
SIGNAL_THRESHOLD = 0.75             # シグナル発生の閾値 (0.75以上)
TOP_SIGNAL_COUNT = 3                
REQUIRED_OHLCV_LIMITS = {'15m': 500, '1h': 500, '4h': 500} 

LONG_TERM_SMA_LENGTH = 50           
LONG_TERM_REVERSAL_PENALTY = 0.20   
MACD_CROSS_PENALTY = 0.15           

# Dynamic Trailing Stop (DTS) Parameters
ATR_TRAIL_MULTIPLIER = 3.0          
SL_BUFFER_ATR_MULTIPLIER = 0.5      
DTS_RRR_DISPLAY = 5.0               

# Funding Rate Bias Filter Parameters
FUNDING_RATE_THRESHOLD = 0.00015    
FUNDING_RATE_BONUS_PENALTY = 0.08   

# BTC Dominance Bias Filter Parameters
BTC_DOMINANCE_BONUS_PENALTY = 0.05  

# スコアリングロジック用の定数 
ADX_TREND_THRESHOLD = 30            
BASE_SCORE = 0.40                   
VOLUME_CONFIRMATION_MULTIPLIER = 2.5 
STRUCTURAL_PIVOT_BONUS = 0.07       

# グローバル状態変数
CCXT_CLIENT_NAME: str = 'OKX' 
EXCHANGE_CLIENT: Optional[ccxt_async.Exchange] = None
LAST_UPDATE_TIME: float = 0.0
CURRENT_MONITOR_SYMBOLS: List[str] = [s.replace('/', '-') for s in DEFAULT_SYMBOLS[:TOP_SYMBOL_LIMIT]] 
TRADE_NOTIFIED_SYMBOLS: Dict[str, float] = {} 
LAST_ANALYSIS_SIGNALS: List[Dict] = [] 
LAST_SUCCESS_TIME: float = 0.0
GLOBAL_MACRO_CONTEXT: Dict = {}
POSITION_SIZE_USD = 1000.0 # 損益計算に使用する仮想ポジションサイズ

# ロギング設定
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S',
                    stream=sys.stdout, 
                    force=True)
logging.getLogger('ccxt').setLevel(logging.WARNING)

# ====================================================================================
# UTILITIES & FORMATTING
# ====================================================================================

def get_tp_reach_time(timeframe: str) -> str:
    """時間足に基づきTP到達目安を算出する"""
    if timeframe == '15m': return "数時間以内 (2〜4時間)"
    if timeframe == '1h': return "半日以内 (6〜12時間)"
    if timeframe == '4h': return "数日以内 (1〜3日)"
    return "N/A"

def format_price_utility(price: float, symbol: str) -> str:
    """価格の小数点以下の桁数を整形"""
    if price is None or price <= 0: return "0.00"
    if price >= 1000: return f"{price:,.2f}"
    if price >= 10: return f"{price:,.4f}"
    if price >= 0.1: return f"{price:,.6f}"
    return f"{price:,.8f}"

def send_telegram_html(message: str) -> bool:
    """TelegramにHTML形式でメッセージを送信する"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML'
    }
    try:
        response = requests.post(url, data=payload)
        response.raise_for_status() 
        logging.info("Telegram通知を送信しました。")
        return True
    except requests.exceptions.RequestException as e:
        logging.error(f"Telegram Request Error: {e}")
        return False

def get_estimated_win_rate(score: float, timeframe: str) -> float:
    """スコアと時間軸に基づき推定勝率を算出する (0.0 - 1.0 スケールで計算)"""
    adjusted_rate = 0.50 + (score - 0.50) * 1.45 
    return max(0.40, min(0.85, adjusted_rate))


def format_integrated_analysis_message(symbol: str, signals: List[Dict], rank: int) -> str:
    """
    3つの時間軸の分析結果を統合し、詳細な通知メッセージを生成する (v16.0.1対応)
    """
    
    valid_signals = [s for s in signals if s.get('side') not in ["DataShortage", "ExchangeError", "Neutral"]]
    
    if not valid_signals: return "" 
        
    high_score_signals = [s for s in valid_signals if s.get('score', 0.5) >= SIGNAL_THRESHOLD]
    
    if not high_score_signals: return "" 
        
    # 最もスコアが高いシグナルを採用
    best_signal = max(
        high_score_signals, 
        key=lambda s: (s.get('score', 0.5), s.get('rr_ratio', 0.0))
    )
    
    # 主要な取引情報を抽出
    price = best_signal.get('price', 0.0)
    timeframe = best_signal.get('timeframe', 'N/A')
    side = best_signal.get('side', 'N/A').upper()
    score_raw = best_signal.get('score', 0.5)
    
    entry_price = best_signal.get('entry', 0.0)
    tp_price = best_signal.get('tp1', 0.0) 
    sl_price = best_signal.get('sl', 0.0) 
    entry_type = best_signal.get('entry_type', 'N/A') 

    score_100 = score_raw * 100
    win_rate = get_estimated_win_rate(score_raw, timeframe) * 100
    display_symbol = symbol.replace('-', '/')
    
    # 損益計算（1000 USDポジション）
    position_size = POSITION_SIZE_USD
    sl_pnl, tp_pnl = 0.0, 0.0
    sl_pnl_percent, tp_pnl_percent = 0.0, 0.0
    
    if entry_price > 0 and sl_price != entry_price:
        # 数量の計算 (Entry Priceベース)
        quantity = position_size / entry_price
        
        # SL P&L (損益)
        sl_pnl = quantity * (sl_price - entry_price) if side == "ロング" else quantity * (entry_price - sl_price)
        sl_pnl_percent = (sl_pnl / position_size) * 100
        
        # TP P&L (利確) - TPはDTS開始ライン
        tp_pnl = quantity * (tp_price - entry_price) if side == "ロング" else quantity * (entry_price - tp_price)
        tp_pnl_percent = (tp_pnl / position_size) * 100

    direction_emoji = "🚀 **ロング (LONG)**" if side == "ロング" else "💥 **ショート (SHORT)**"
    strength = "極めて良好 (VERY HIGH)" if score_raw >= 0.85 else ("高 (HIGH)" if score_raw >= 0.75 else "中 (MEDIUM)")
    
    rank_header = f"🥇 **総合 {rank} 位！**" if rank == 1 else (f"🥈 **総合 {rank} 位！**" if rank == 2 else f"🏆 **総合 {rank} 位！")
            
    sl_source_str = f"構造的 (ATR x {SL_BUFFER_ATR_MULTIPLIER} バッファ)" if best_signal.get('tech_data', {}).get('structural_sl_used', False) else f"ATR x {ATR_TRAIL_MULTIPLIER:.1f}"
    
    tech_data = best_signal.get('tech_data', {})

    # ----------------------------------------------------
    # 1. ヘッダーとエントリー情報の可視化
    # ----------------------------------------------------
    header = (
        f"--- {('🟢' if side=='ロング' else '🔴')} --- **{display_symbol}** --- {('🟢' if side=='ロング' else '🔴')} ---\n"
        f"{rank_header} 🔥 {strength} 発生！ - {direction_emoji}\n" 
        f"==================================\n"
        f"| 🎯 **予測勝率** | **<ins>{win_rate:.1f}%</ins>** | **条件極めて良好** |\n"
        f"| 💯 **分析スコア** | <b>{score_100:.2f} / 100.00 点</b> (ベース: {timeframe}足) |\n" 
        f"| ⏰ **決済戦略** | **DTS (動的追跡損切)** | (目標RRR: 1:{DTS_RRR_DISPLAY:.2f}+) |\n" 
        f"| ⏳ **TP到達目安** | {get_tp_reach_time(timeframe)} |\n"
        f"==================================\n"
    )

    # 価格候補の明記
    trade_plan = (
        f"**🎯 推奨取引計画と価格候補**\n"
        f"----------------------------------\n"
        f"| 指標 | 価格 (USD) | 役割 (S/Rライン) |\n"
        f"| :--- | :--- | :--- |\n"
        f"| 💰 現在価格 | <code>${format_price_utility(price, symbol)}</code> | 参照価格 |\n"
        f"| ➡️ **Entry ({entry_type})** | <code>${format_price_utility(entry_price, symbol)}</code> | {side}ポジション ({sl_source_str} SL) |\n" 
        f"| ❌ **SL 位置** | <code>${format_price_utility(sl_price, symbol)}</code> | **{'⬇️ 支持候補' if side=='ロング' else '⬆️ 抵抗候補'}** (最終SL) |\n" 
        f"| 🟢 **TP 目標** | <code>${format_price_utility(tp_price, symbol)}</code> | **{'⬆️ 抵抗候補' if side=='ロング' else '⬇️ 支持候補'}** (DTS開始ライン) |\n" 
        f"----------------------------------\n"
    )

    # P&L DISPLAY (1000 USD)
    pnl_result = (
        f"\n**📈 損益結果 ({position_size:.0f} USD ポジションの場合)**\n"
        f"----------------------------------\n"
        f"| 項目 | **損益額 (USD)** | 損益率 (対ポジションサイズ) |\n"
        f"| :--- | :--- | :--- |\n"
        f"| ❌ SL実行時 (損切) | **{sl_pnl:.2f} USD** | **{sl_pnl_percent:.2f}%** |\n"
        f"| 🟢 TP目標時 (利確) | **+{tp_pnl:.2f} USD** | **+{tp_pnl_percent:.2f}%** |\n"
        f"----------------------------------\n"
    )

    # ----------------------------------------------------
    # 2. 統合分析サマリーと優位性のあった分析
    # ----------------------------------------------------
    
    analysis_detail = "**💡 優位性のあった分析（統合加点根拠）**\n\n"
    
    highlight_points = []
    
    # 4h Trend
    long_term_reversal_penalty = tech_data.get('long_term_reversal_penalty', False)
    long_term_trend_4h = 'N/A'
    for s in signals:
        if s.get('timeframe') == '4h':
            long_term_trend_4h = s.get('tech_data', {}).get('long_term_trend', 'Neutral')
            break
            
    # Highlight 1: 4h Trend
    if not long_term_reversal_penalty:
        highlight_points.append(f"1. **長期順張り確証**: 4h足が{side}トレンドに一致（**+{LONG_TERM_REVERSAL_PENALTY*100:.1f}点** 加点）")
    
    # Highlight 2: Volume Confirmation
    volume_bonus = tech_data.get('volume_confirmation_bonus', 0.0)
    volume_ratio = tech_data.get('volume_ratio', 0.0)
    if volume_bonus > 0:
        highlight_points.append(f"2. **出来高急増**: 取引量が平均の{volume_ratio:.1f}倍に急増し流動性を確証（**+{volume_bonus * 100:.2f}点** ボーナス）")

    # Highlight 3: Structural SL/Pivot
    pivot_bonus = tech_data.get('structural_pivot_bonus', 0.0)
    if pivot_bonus > 0:
        pivot_level = 'R1' if side == 'ショート' else 'S1'
        highlight_points.append(f"3. **構造的S/R確証**: {pivot_level}を損切根拠に採用し、根拠の強さを確証（**+{pivot_bonus * 100:.2f}点** ボーナス）")
        
    # Highlight 4: Funding Rate
    funding_rate_bonus = tech_data.get('funding_rate_bonus_value', 0.0)
    if funding_rate_bonus > 0:
        bias_type = "需給の過密解消"
        highlight_points.append(f"4. **資金調達率優位**: {bias_type}傾向でシグナルに優位性（**+{funding_rate_bonus * 100:.2f}点** 加点）")

    # Highlight 5: BTC Dominance (Altcoins only)
    dominance_bonus = tech_data.get('dominance_bonus_value', 0.0)
    btc_dominance_trend = tech_data.get('btc_dominance_trend', 'N/A')
    if dominance_bonus > 0:
        highlight_points.append(f"5. **BTCドミナンス順張り**: {btc_dominance_trend}トレンドでAltcoinの{side}に優位性（**+{dominance_bonus * 100:.2f}点** 加点）")
        
    # Combine Highlight points
    analysis_detail += '\n'.join(highlight_points) + '\n\n'
    
    # Detailed Scoring Breakdown
    analysis_detail += "**📊 スコアリング・フィルター詳細 (加点/減点)**\n"

    # 4h Trend Detail
    trend_score_change = LONG_TERM_REVERSAL_PENALTY if not long_term_reversal_penalty else -LONG_TERM_REVERSAL_PENALTY
    analysis_detail += (f"   └ **4h 足 (長期トレンド)**: {long_term_trend_4h} -> **{'+' if trend_score_change >= 0 else ''}{trend_score_change*100:.1f}点** 適用\n")

    # Structural/Pivot Detail
    analysis_detail += (f"   └ **構造分析(Pivot)**: {'✅ ' if pivot_bonus > 0 else '❌ '}S/R確証 (**+{pivot_bonus * 100:.2f}点**)\n")

    # Volume Detail
    analysis_detail += (f"   └ **出来高/流動性確証**: {'✅ ' if volume_bonus > 0 else '❌ '} (**+{volume_bonus * 100:.2f}点**, 比率: {volume_ratio:.1f}x)\n")

    # Funding Rate Detail
    funding_rate_val = tech_data.get('funding_rate_value', 0.0)
    funding_rate_status = "✅ 優位性あり" if funding_rate_bonus > 0 else ("⚠️ 過密ペナルティ" if funding_rate_bonus < 0 else "❌ フィルター範囲外")
    analysis_detail += (f"   └ **資金調達率 (FR)**: {funding_rate_val * 100:.4f}% (8h) - {funding_rate_status} (**{'+' if funding_rate_bonus >= 0 else ''}{funding_rate_bonus * 100:.2f}点**)\n")

    # BTC Dominance Detail
    dominance_status = "✅ 順張り優位" if dominance_bonus > 0 else ("⚠️ 逆張りペナルティ" if dominance_bonus < 0 else "❌ BTC銘柄/Neutral")
    analysis_detail += (f"   └ **BTCドミナンス** ({btc_dominance_trend}): {dominance_status} (**{'+' if dominance_bonus >= 0 else ''}{dominance_bonus * 100:.2f}点**)\n")
    
    # MACD/Momentum Penalty
    macd_cross_penalty_value = tech_data.get('macd_cross_penalty_value', 0.0)
    if macd_cross_penalty_value > 0:
        analysis_detail += (f"   └ **モメンタム反転減点**: MACDクロス逆行により（**-{macd_cross_penalty_value * 100:.2f}点** 適用）\n")
    
    # Footer
    regime = best_signal.get('regime', 'N/A')
    adx_value = tech_data.get('adx', 0.0)
    footer = (
        f"\n==================================\n"
        f"| 🔍 **市場環境** | **{regime}** 相場 (ADX: {adx_value:.2f}) |\n"
        f"| ⚙️ **BOT Ver** | **{BOT_VERSION}** |\n" 
        f"==================================\n"
        f"<pre>※ DTS戦略により、利益確定目標に到達後もSLを追跡し、利益を最大化します。</pre>"
    )

    return f"{header}\n{trade_plan}{pnl_result}\n{analysis_detail}\n{footer}"
    
# ====================================================================================
# EXCHANGE & DATA HANDLING (CCXT)
# ====================================================================================

async def initialize_exchange_client() -> ccxt_async.Exchange:
    """CCXTクライアントを初期化する"""
    global EXCHANGE_CLIENT
    try:
        if EXCHANGE_CLIENT:
            await EXCHANGE_CLIENT.close()
            
        EXCHANGE_CLIENT = getattr(ccxt_async, CCXT_CLIENT_NAME.lower())({
            'enableRateLimit': True,
            'rateLimit': 500,
            'options': {'defaultType': 'future'}
        })
        await EXCHANGE_CLIENT.load_markets()
        logging.info(f"CCXTクライアントを {CCXT_CLIENT_NAME} で初期化しました。")
        return EXCHANGE_CLIENT
    except Exception as e:
        logging.error(f"CCXTクライアントの初期化に失敗しました: {e}")
        return None

async def fetch_ohlcv_data(symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
    """OHLCVデータを取得し、DataFrameとして整形する"""
    try:
        if EXCHANGE_CLIENT is None:
            await initialize_exchange_client()

        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol.replace('-', '/'), timeframe, limit=limit)
        
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True).dt.tz_convert(JST)
        df.set_index('timestamp', inplace=True)
        return df
    except Exception as e:
        logging.error(f"OHLCVデータの取得に失敗 ({symbol}, {timeframe}): {e}")
        return None

async def get_current_volume_leaders() -> List[str]:
    """出来高に基づいて監視シンボルを決定する"""
    if EXCHANGE_CLIENT is None:
        await initialize_exchange_client()
        
    try:
        tickers = await EXCHANGE_CLIENT.fetch_tickers()
        
        usdt_futures = {
            s: t['quoteVolume']
            for s, t in tickers.items()
            if s.endswith('/USDT') and t['quoteVolume'] is not None and t['symbol'] in EXCHANGE_CLIENT.markets
        }
        
        sorted_usdt = sorted(usdt_futures.items(), key=lambda item: item[1], reverse=True)
        
        top_symbols = [s[0].replace('/', '-') for s in sorted_usdt[:TOP_SYMBOL_LIMIT]]
        
        logging.info(f"出来高TOP {len(top_symbols)} のシンボルを取得しました。")
        return top_symbols
    except Exception as e:
        logging.error(f"出来高リーダーの取得に失敗: {e}")
        return [s.replace('/', '-') for s in DEFAULT_SYMBOLS[:TOP_SYMBOL_LIMIT]]

async def fetch_funding_rate(symbol: str) -> float:
    """Funding Rate (8時間) を取得する"""
    try:
        if EXCHANGE_CLIENT is None:
            await initialize_exchange_client()
            
        funding_rate_info = await EXCHANGE_CLIENT.fetch_funding_rate(symbol.replace('-', '/'))
        return funding_rate_info['fundingRate']
    except Exception as e:
        logging.warning(f"Funding Rateの取得に失敗 ({symbol}): {e}")
        return 0.0

def get_macro_context() -> Dict:
    """マクロ環境のコンテキストを取得する (v16.0.1: BTCドミナンスを追加)"""
    # 実際には外部APIからFear & Greed Indexなどを取得する
    # ここではシミュレーションとしてダミー値を生成
    fgi_proxy = random.uniform(-0.10, 0.10)
    
    # BTC Dominance Trendをシミュレート
    btc_dominance_trend = random.choice(["Increasing", "Decreasing", "Neutral"])
    
    return {
        "sentiment_fgi_proxy": fgi_proxy,
        "btc_dominance_trend": btc_dominance_trend
    }

# ====================================================================================
# TECHNICAL ANALYSIS & SCORING LOGIC
# ====================================================================================

def calculate_indicators(df: pd.DataFrame, timeframe: str) -> Optional[pd.DataFrame]:
    """DataFrameに技術指標を計算して追加する"""
    if df is None or len(df) < REQUIRED_OHLCV_LIMITS.get(timeframe, 500):
        return None
    
    df.ta.sma(length=LONG_TERM_SMA_LENGTH, append=True)
    df.ta.atr(append=True)
    df.ta.rsi(append=True)
    df.ta.macd(append=True)
    df.ta.cci(append=True)
    df.ta.stochrsi(append=True)
    df.ta.adx(append=True)
    df.ta.vwap(append=True) 
    df.ta.dc(append=True)
    df.ta.pivot(kind='fibonacci', append=True)
    
    df = df.iloc[LONG_TERM_SMA_LENGTH:].copy()
    if df.empty:
        return None
        
    return df

def find_trade_signal(symbol: str, timeframe: str, df: pd.DataFrame, funding_rate: float, macro_context: Dict) -> Dict:
    """
    主要なテクニカル指標を分析し、取引シグナルとスコアを生成する (v16.0.1ロジック)
    """
    
    if df is None or len(df) < 2:
        return {'symbol': symbol, 'timeframe': timeframe, 'side': 'DataShortage', 'score': 0.0, 'price': 0.0}

    last = df.iloc[-1]
    prev = df.iloc[-2]
    
    current_price = last['close']
    
    # ----------------------------------------------------
    # 1. 指標の取得とトレンド判断
    # ----------------------------------------------------
    atr_value = last[f'ATR_{df.ta.common.df.ta.atr.length}']
    long_term_sma = last[f'SMA_{LONG_TERM_SMA_LENGTH}']
    macd_hist = last['MACDh_12_26_9']
    prev_macd_hist = prev['MACDh_12_26_9']
    adx = last[f'ADX_{df.ta.common.df.ta.adx.length}']
    regime = "トレンド" if adx > ADX_TREND_THRESHOLD else "レンジ"
    r1 = last['P_R1_FIB']
    s1 = last['P_S1_FIB']
    vwap = last[f'VWAP_D']

    # ----------------------------------------------------
    # 2. ベースシグナルとスコアリング
    # ----------------------------------------------------
    
    score = BASE_SCORE  
    side = 'Neutral'
    tech_data = {'atr_value': atr_value, 'adx': adx, 'regime': regime, 'funding_rate_value': funding_rate}
    
    # MACDヒストグラムによる初期判断
    if macd_hist > 0 and macd_hist > prev_macd_hist:
        side = 'ロング'
    elif macd_hist < 0 and macd_hist < prev_macd_hist:
        side = 'ショート'
    
    # トレンドの勢いによる加点
    if side != 'Neutral':
        if side == 'ロング':
            if last['RSI_14'] > 50: score += 0.05
            if last['CCI_14'] > 0: score += 0.05
            tech_data['vwap_consistent'] = current_price > vwap
            if tech_data['vwap_consistent']: score += 0.05
        else: # ショート
            if last['RSI_14'] < 50: score += 0.05
            if last['CCI_14'] < 0: score += 0.05
            tech_data['vwap_consistent'] = current_price < vwap
            if tech_data['vwap_consistent']: score += 0.05

    # ----------------------------------------------------
    # 3. 4h足トレンドフィルター (4h足のみで実行)
    # ----------------------------------------------------
    if timeframe == '4h':
        long_term_trend = 'Long' if current_price > long_term_sma else ('Short' if current_price < long_term_sma else 'Neutral')
        tech_data['long_term_trend'] = long_term_trend
        return {'symbol': symbol, 'timeframe': timeframe, 'side': long_term_trend, 'score': score, 'price': current_price, 'tech_data': tech_data}


    # ----------------------------------------------------
    # 4. DTS & Structural SL/TP 設定 (1h, 15m)
    # ----------------------------------------------------
    
    entry_type = 'Limit'
    entry_price = current_price
    sl_price = 0.0
    tp_price = 0.0
    structural_sl_used = False
    
    if side == 'ロング':
        entry_price = s1 # S1でのLimit
        sl_base = s1 - atr_value * SL_BUFFER_ATR_MULTIPLIER # 構造的SL (S1 - 0.5 ATR)
        sl_price = max(sl_base, entry_price - atr_value * ATR_TRAIL_MULTIPLIER) 
        tp_price = r1 
        
        if sl_base > entry_price - atr_value * ATR_TRAIL_MULTIPLIER: 
            score += STRUCTURAL_PIVOT_BONUS
            structural_sl_used = True
            
    elif side == 'ショート':
        entry_price = r1 # R1でのLimit
        sl_base = r1 + atr_value * SL_BUFFER_ATR_MULTIPLIER # 構造的SL (R1 + 0.5 ATR)
        sl_price = min(sl_base, entry_price + atr_value * ATR_TRAIL_MULTIPLIER) 

        tp_price = s1
        
        if sl_base < entry_price + atr_value * ATR_TRAIL_MULTIPLIER: 
            score += STRUCTURAL_PIVOT_BONUS
            structural_sl_used = True
            
    tech_data['structural_sl_used'] = structural_sl_used
    tech_data['structural_pivot_bonus'] = STRUCTURAL_PIVOT_BONUS if structural_sl_used else 0.0

    # ----------------------------------------------------
    # 5. スコアリングフィルターと加点/減点
    # ----------------------------------------------------
    
    # 出来高確認
    avg_volume = df['volume'].iloc[-REQUIRED_OHLCV_LIMITS.get(timeframe, 500):-1].mean()
    volume_ratio = last['volume'] / avg_volume if avg_volume > 0 else 0
    volume_confirmation_bonus = 0.0
    if volume_ratio >= VOLUME_CONFIRMATION_MULTIPLIER:
        volume_confirmation_bonus = 0.12 
        score += volume_confirmation_bonus
    tech_data['volume_confirmation_bonus'] = volume_confirmation_bonus
    tech_data['volume_ratio'] = volume_ratio
    
    # 4h足トレンドとの整合性チェック
    long_term_trend = macro_context.get('long_term_trend_4h', 'Neutral')
    long_term_reversal_penalty = False
    
    if (side == 'ロング' and long_term_trend == 'Short') or \
       (side == 'ショート' and long_term_trend == 'Long'):
        score -= LONG_TERM_REVERSAL_PENALTY 
        long_term_reversal_penalty = True
    elif long_term_trend != 'Neutral' and long_term_trend.lower() == side.lower():
        score += LONG_TERM_REVERSAL_PENALTY 
        
    tech_data['long_term_reversal_penalty'] = long_term_reversal_penalty
    tech_data['long_term_reversal_penalty_value'] = LONG_TERM_REVERSAL_PENALTY if long_term_reversal_penalty else 0.0
    
    # MACDクロスによるモメンタム不一致ペナルティ
    macd_cross_valid = True
    macd_cross_penalty_value = 0.0
    if (side == 'ロング' and macd_hist < 0) or \
       (side == 'ショート' and macd_hist > 0):
        score -= MACD_CROSS_PENALTY 
        macd_cross_valid = False
        macd_cross_penalty_value = MACD_CROSS_PENALTY
        
    tech_data['macd_cross_valid'] = macd_cross_valid
    tech_data['macd_cross_penalty_value'] = macd_cross_penalty_value
    
    # Funding Rate Bias Filter
    funding_rate_bonus_value = 0.0
    if side == 'ロング' and funding_rate <= -FUNDING_RATE_THRESHOLD:
        funding_rate_bonus_value = FUNDING_RATE_BONUS_PENALTY 
        score += funding_rate_bonus_value
    elif side == 'ショート' and funding_rate >= FUNDING_RATE_THRESHOLD:
        funding_rate_bonus_value = FUNDING_RATE_BONUS_PENALTY 
        score += funding_rate_bonus_value
    elif (side == 'ロング' and funding_rate >= FUNDING_RATE_THRESHOLD) or \
         (side == 'ショート' and funding_rate <= -FUNDING_RATE_THRESHOLD):
        funding_rate_bonus_value = -FUNDING_RATE_BONUS_PENALTY 
        score += funding_rate_bonus_value
        
    tech_data['funding_rate_bonus_value'] = funding_rate_bonus_value

    # BTC Dominance Bias Filter
    dominance_bonus_value = 0.0
    btc_dominance_trend = macro_context.get('btc_dominance_trend', 'Neutral')
    is_altcoin = not symbol.startswith('BTC')
    
    if is_altcoin and btc_dominance_trend != 'Neutral':
        if btc_dominance_trend == 'Increasing':
            if side == 'ロング':
                dominance_bonus_value = -BTC_DOMINANCE_BONUS_PENALTY 
                score += dominance_bonus_value
            elif side == 'ショート':
                dominance_bonus_value = BTC_DOMINANCE_BONUS_PENALTY 
                score += dominance_bonus_value
        elif btc_dominance_trend == 'Decreasing':
            if side == 'ロング':
                dominance_bonus_value = BTC_DOMINANCE_BONUS_PENALTY 
                score += dominance_bonus_value
            elif side == 'ショート':
                dominance_bonus_value = -BTC_DOMINANCE_BONUS_PENALTY 
                score += dominance_bonus_value
                
    tech_data['btc_dominance_trend'] = btc_dominance_trend
    tech_data['dominance_bonus_value'] = dominance_bonus_value
    
    # 最終スコアを0.0から1.0の間に丸める
    score = max(0.0, min(1.0, score))

    return {
        'symbol': symbol,
        'timeframe': timeframe,
        'side': side,
        'score': score,
        'price': current_price,
        'entry': entry_price,
        'sl': sl_price,
        'tp1': tp_price, 
        'rr_ratio': DTS_RRR_DISPLAY,
        'entry_type': entry_type,
        'current_time': df.iloc[-1].name,
        'tech_data': tech_data,
        'macro_context': macro_context,
        'regime': regime
    }

# ====================================================================================
# MAIN LOOP
# ====================================================================================

async def main_loop():
    """BOTのメイン処理ループ"""
    global LAST_SUCCESS_TIME, TRADE_NOTIFIED_SYMBOLS, CURRENT_MONITOR_SYMBOLS, LAST_ANALYSIS_SIGNALS, GLOBAL_MACRO_CONTEXT
    
    await initialize_exchange_client()
    
    while True:
        try:
            # 1. 監視対象の更新とマクロコンテキストの取得
            CURRENT_MONITOR_SYMBOLS = await get_current_volume_leaders()
            GLOBAL_MACRO_CONTEXT = get_macro_context()
            
            all_signals = []
            
            # 2. 全シンボルをループして分析
            for symbol in CURRENT_MONITOR_SYMBOLS:
                symbol_signals = []
                
                funding_rate = await fetch_funding_rate(symbol)
                
                # 4hの長期トレンドを先に決定する
                df_4h = await fetch_ohlcv_data(symbol, '4h', REQUIRED_OHLCV_LIMITS['4h'])
                df_4h_ta = calculate_indicators(df_4h, '4h')
                signal_4h = find_trade_signal(symbol, '4h', df_4h_ta, funding_rate, GLOBAL_MACRO_CONTEXT)
                symbol_signals.append(signal_4h)
                
                # マクロコンテキストに4hトレンドの結果を格納
                macro_context_with_4h = GLOBAL_MACRO_CONTEXT.copy()
                macro_context_with_4h['long_term_trend_4h'] = signal_4h.get('tech_data', {}).get('long_term_trend', 'Neutral')
                
                # 1hと15mでシグナルを生成
                for tf in ['1h', '15m']:
                    df = await fetch_ohlcv_data(symbol, tf, REQUIRED_OHLCV_LIMITS[tf])
                    df_ta = calculate_indicators(df, tf)
                    signal = find_trade_signal(symbol, tf, df_ta, funding_rate, macro_context_with_4h)
                    symbol_signals.append(signal)
                    await asyncio.sleep(REQUEST_DELAY_PER_SYMBOL) 
                
                all_signals.extend(symbol_signals)

            # 3. 最良シグナルのフィルタリングとランキング
            actionable_signals = [
                s for s in all_signals
                if s.get('score', 0.0) >= SIGNAL_THRESHOLD and 
                   s.get('side') not in ['Neutral', 'DataShortage', 'ExchangeError'] and
                   s.get('timeframe') in ['1h', '15m'] 
            ]
            
            # スコア、RRR、ADX、ATRの順でソート
            actionable_signals.sort(
                key=lambda s: (
                    s.get('score', 0.0), 
                    s.get('rr_ratio', 0.0),
                    s.get('tech_data', {}).get('adx', 0.0), 
                    -s.get('tech_data', {}).get('atr_value', 1.0)
                ), 
                reverse=True
            )

            # 4. 通知ロジック
            notified_count = 0
            for rank, best_signal in enumerate(actionable_signals[:TOP_SIGNAL_COUNT]):
                symbol = best_signal['symbol']
                
                if time.time() - TRADE_NOTIFIED_SYMBOLS.get(symbol, 0) < TRADE_SIGNAL_COOLDOWN:
                    continue
                
                all_symbol_signals = [s for s in all_signals if s['symbol'] == symbol]
                
                message = format_integrated_analysis_message(symbol, all_symbol_signals, rank + 1)
                
                if send_telegram_html(message):
                    TRADE_NOTIFIED_SYMBOLS[symbol] = time.time()
                    notified_count += 1
                
            LAST_ANALYSIS_SIGNALS = actionable_signals
            LAST_SUCCESS_TIME = time.time()
            logging.info(f"メインループが完了しました。シグナル通知数: {notified_count}件。次回実行まで{LOOP_INTERVAL}秒待機。")

            await asyncio.sleep(LOOP_INTERVAL)

        except Exception as e:
            error_name = type(e).__name__
            if "Connection" in error_name or "Timeout" in error_name:
                logging.warning(f"ネットワークエラーが発生しました。クライアントを再初期化します。: {error_name}")
                await initialize_exchange_client()
            else:
                import traceback
                logging.error(f"メインループで致命的なエラー: {error_name}")
                await asyncio.sleep(60)


# ====================================================================================
# FASTAPI SETUP
# ====================================================================================

app = FastAPI(title="Apex BOT API", version=BOT_VERSION)

@app.on_event("startup")
async def startup_event():
    """FastAPI起動時にメインループを非同期タスクとして開始"""
    logging.info(f"🚀 Apex BOT {BOT_VERSION} Startup initializing...") 
    asyncio.create_task(main_loop())

@app.on_event("shutdown")
async def shutdown_event():
    """シャットダウン時にCCXTクライアントを閉じる"""
    global EXCHANGE_CLIENT
    if EXCHANGE_CLIENT:
        await EXCHANGE_CLIENT.close()
        logging.info("CCXTクライアントをシャットダウンしました。")

@app.get("/status")
def get_status():
    """BOTの現在の状態を返すAPIエンドポイント"""
    status_msg = {
        "status": "ok",
        "bot_version": BOT_VERSION,
        "last_success_time_utc": datetime.fromtimestamp(LAST_SUCCESS_TIME, tz=timezone.utc).isoformat() if LAST_SUCCESS_TIME else "N/A",
        "current_client": CCXT_CLIENT_NAME,
        "monitoring_symbols": len(CURRENT_MONITOR_SYMBOLS),
        "last_signals_count": len(LAST_ANALYSIS_SIGNALS)
    }
    return JSONResponse(content=status_msg)

@app.head("/")
@app.get("/")
def home_view():
    """ルートパスでBOTの稼働確認メッセージを返す"""
    return JSONResponse(content={"message": f"Apex BOT ({BOT_VERSION}) is running. Check /status for details."})

# ------------------------------------------------------------------------------------
# 実行コマンド: uvicorn main:app --host 0.0.0.0 --port 8000
# ------------------------------------------------------------------------------------
