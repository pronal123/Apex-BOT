# main_render.py (ValueError Fix - v16.0.4)
# ====================================================================================
# Apex BOT v16.0.4 - ValueError Fix (Robust YFinance Data Handling)
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
SIGNAL_THRESHOLD = 0.75             # 閾値を 0.75 に設定
TOP_SIGNAL_COUNT = 3                
REQUIRED_OHLCV_LIMITS = {'15m': 500, '1h': 500, '4h': 500} 
VOLATILITY_BB_PENALTY_THRESHOLD = 5.0 

LONG_TERM_SMA_LENGTH = 50           
LONG_TERM_REVERSAL_PENALTY = 0.20   
MACD_CROSS_PENALTY = 0.15           

# Dynamic Trailing Stop (DTS) Parameters
ATR_TRAIL_MULTIPLIER = 3.0          
DTS_RRR_DISPLAY = 5.0               

# Funding Rate Bias Filter Parameters
FUNDING_RATE_THRESHOLD = 0.00015    
FUNDING_RATE_BONUS_PENALTY = 0.08   

# Dominance Bias Filter Parameters
DOMINANCE_BIAS_BONUS_PENALTY = 0.05 # BTCドミナンスの偏りによる最大ボーナス/ペナルティ点

# スコアリングロジック用の定数 
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
RSI_MOMENTUM_LOW = 40               
RSI_MOMENTUM_HIGH = 60              
ADX_TREND_THRESHOLD = 30            
BASE_SCORE = 0.40                   
VOLUME_CONFIRMATION_MULTIPLIER = 2.5 

# 🚨 NEW: 損益計算用の固定資本
POSITION_CAPITAL = 1000.0 # $1000の固定資本 (レバレッジ1倍を想定)

# グローバル状態変数
CCXT_CLIENT_NAME: str = 'OKX' 
EXCHANGE_CLIENT: Optional[ccxt_async.Exchange] = None
LAST_UPDATE_TIME: float = 0.0
CURRENT_MONITOR_SYMBOLS: List[str] = [s.replace('/', '-') for s in DEFAULT_SYMBOLS[:TOP_SYMBOL_LIMIT]] 
TRADE_NOTIFIED_SYMBOLS: Dict[str, float] = {} 
LAST_ANALYSIS_SIGNALS: List[Dict] = [] 
LAST_SUCCESS_TIME: float = 0.0
LAST_SUCCESSFUL_MONITOR_SYMBOLS: List[str] = CURRENT_MONITOR_SYMBOLS.copy()
GLOBAL_MACRO_CONTEXT: Dict = {}

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
    """時間足に基づきTP到達目安を算出する (ログメッセージ用)"""
    if timeframe == '15m': return "数時間以内 (2〜8時間)"
    if timeframe == '1h': return "半日以内 (6〜24時間)"
    if timeframe == '4h': return "数日以内 (2〜7日)"
    return "N/A"

def format_price_utility(price: float, symbol: str) -> str:
    """価格の小数点以下の桁数を整形"""
    if price is None or price <= 0: return "0.00"
    if price >= 1000: return f"{price:,.2f}"
    if price >= 10: return f"{price:,.4f}"
    if price >= 0.1: return f"{price:,.6f}"
    return f"{price:,.8f}"

# 🚨 NEW UTILITY: 損益計算関数
def calculate_pnl(entry_price: float, target_price: float, side: str) -> float:
    """
    $1000のポジションサイズでの概算損益を計算する (1xレバレッジを想定)。
    """
    if entry_price <= 0 or target_price <= 0:
        return 0.0
    
    # ポジションサイズ (トークン量) = 資本 / エントリー価格
    position_size_token = POSITION_CAPITAL / entry_price
    
    # 損益を計算
    price_diff = target_price - entry_price
    
    if side == 'ロング':
        pnl = position_size_token * price_diff
    elif side == 'ショート':
        # ショートの場合、利益は価格下落時
        pnl = position_size_token * (-price_diff) 
    else:
        return 0.0
        
    return pnl

# 🚨 NEW UTILITY: Telegram向け損益整形関数
def format_pnl_utility_telegram(pnl: float) -> str:
    """損益を整形し、Telegramで表示可能な絵文字と太字で色付けする"""
    if pnl > 0.0:
        return f"🟢 **+${pnl:,.2f}**"
    elif pnl < 0.0:
        return f"🔴 **-${abs(pnl):,.2f}**"
    return f"${pnl:,.2f}"


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
    except requests.exceptions.HTTPError as e:
        logging.error(f"Telegram HTTP Error: {e.response.text if e.response else 'N/A'}")
        return False
    except requests.exceptions.RequestException as e:
        logging.error(f"Telegram Request Error: {e}")
        return False

def get_estimated_win_rate(score: float, timeframe: str) -> float:
    """スコアと時間軸に基づき推定勝率を算出する (0.0 - 1.0 スケールで計算)"""
    # 調整ロジックは保持
    adjusted_rate = 0.50 + (score - 0.50) * 1.45 
    return max(0.40, min(0.85, adjusted_rate))


def format_integrated_analysis_message(symbol: str, signals: List[Dict], rank: int) -> str:
    """
    3つの時間軸の分析結果を統合し、ログメッセージの形式に整形する (v16.0.4対応)
    """
    
    valid_signals = [s for s in signals if s.get('side') not in ["DataShortage", "ExchangeError", "Neutral"]]
    
    if not valid_signals:
        return "" 
        
    high_score_signals = [s for s in valid_signals if s.get('score', 0.5) >= SIGNAL_THRESHOLD]
    
    if not high_score_signals:
        return "" 
        
    # 最もスコアが高いシグナルを採用
    best_signal = max(
        high_score_signals, 
        key=lambda s: (
            s.get('score', 0.5), 
            s.get('rr_ratio', 0.0), 
            s.get('tech_data', {}).get('adx', 0.0), 
            -s.get('tech_data', {}).get('atr_value', 1.0),
            s.get('symbol', '')
        )
    )
    
    # 主要な取引情報を抽出
    price = best_signal.get('price', 0.0)
    timeframe = best_signal.get('timeframe', 'N/A')
    side = best_signal.get('side', 'N/A').upper()
    score_raw = best_signal.get('score', 0.5)
    rr_ratio = best_signal.get('rr_ratio', 0.0)
    
    entry_price = best_signal.get('entry', 0.0)
    tp_price = best_signal.get('tp1', 0.0) # DTS採用のため、これはあくまで遠い目標値
    sl_price = best_signal.get('sl', 0.0) # 初期の追跡ストップ/損切位置
    entry_type = best_signal.get('entry_type', 'N/A') 

    score_100 = score_raw * 100
    win_rate = get_estimated_win_rate(score_raw, timeframe) * 100
    display_symbol = symbol.replace('-', '/')
    
    # リスク幅を計算 (初期のストップ位置との差)
    sl_width = abs(entry_price - sl_price)
    
    # ----------------------------------------------------
    # 🚨 P/Lシミュレーション用のPivot Point値とP/Lの計算
    # ----------------------------------------------------
    tech_data = best_signal.get('tech_data', {})
    
    pivot_s1 = tech_data.get('pivot_s1', 0.0)
    pivot_s2 = tech_data.get('pivot_s2', 0.0)
    pivot_r1 = tech_data.get('pivot_r1', 0.0)
    pivot_r2 = tech_data.get('pivot_r2', 0.0)
    
    # 損益計算 (P/L)
    pnl_s1 = calculate_pnl(entry_price, pivot_s1, side)
    pnl_s2 = calculate_pnl(entry_price, pivot_s2, side)
    pnl_r1 = calculate_pnl(entry_price, pivot_r1, side)
    pnl_r2 = calculate_pnl(entry_price, pivot_r2, side)
    # ----------------------------------------------------

    # ----------------------------------------------------
    # 1. ヘッダーとエントリー情報の可視化
    # ----------------------------------------------------
    direction_emoji = "🚀 **ロング (LONG)**" if side == "ロング" else "💥 **ショート (SHORT)**"
    strength = "極めて良好 (VERY HIGH)" if score_raw >= 0.85 else ("高 (HIGH)" if score_raw >= 0.75 else "中 (MEDIUM)")
    
    rank_header = ""
    if rank == 1: rank_header = "🥇 **総合 1 位！**"
    elif rank == 2: rank_header = "🥈 **総合 2 位！**"
    elif rank == 3: rank_header = "🥉 **総合 3 位！**"
    else: rank_header = f"🏆 **総合 {rank} 位！**"

    market_sentiment_str = ""
    macro_sentiment = best_signal.get('macro_context', {}).get('sentiment_fgi_proxy', 0.0)
    if macro_sentiment >= 0.05:
         market_sentiment_str = " (リスクオン傾向)"
    elif macro_sentiment <= -0.05:
         market_sentiment_str = " (リスクオフ傾向)"
    
    # 決済戦略の表示をDTSに変更
    exit_type_str = "DTS (動的追跡損切)" 
    
    # TP到達目安を追加
    time_to_tp = get_tp_reach_time(timeframe)

    header = (
        f"--- 🟢 --- **{display_symbol}** --- 🟢 ---\n"
        f"{rank_header} 📈 {strength} 発生！ - {direction_emoji}{market_sentiment_str}\n" 
        f"==================================\n"
        f"| 🎯 **予測勝率** | **<ins>{win_rate:.1f}%</ins>** | **条件極めて良好** |\n"
        f"| 💯 **分析スコア** | <b>{score_100:.2f} / 100.00 点</b> (ベース: {timeframe}足) |\n" 
        f"| ⏰ **決済戦略** | **{exit_type_str}** (目標RRR: 1:{rr_ratio:.2f}+) |\n" 
        f"| ⏳ **TP到達目安** | **{time_to_tp}** | (変動する可能性があります) |\n"
        f"==================================\n"
    )

    sl_source_str = "ATR基準"
    if best_signal.get('tech_data', {}).get('structural_sl_used', False):
        sl_source_str = "構造的 (Pivot) + **0.5 ATR バッファ**" # FIX反映
        
    # 取引計画の表示をDTSに合わせて変更
    trade_plan = (
        f"**🎯 推奨取引計画 (Dynamic Trailing Stop & Structural SL)**\n"
        f"----------------------------------\n"
        f"| 指標 | 価格 (USD) | 備考 |\n"
        f"| :--- | :--- | :--- |\n"
        f"| 💰 現在価格 | <code>${format_price_utility(price, symbol)}</code> | 参照価格 |\n"
        f"| ➡️ **Entry ({entry_type})** | <code>${format_price_utility(entry_price, symbol)}</code> | {side}ポジション (**<ins>底/天井を狙う Limit 注文</ins>**) |\n" 
        f"| 📉 **Risk (SL幅)** | ${format_price_utility(sl_width, symbol)} | **初動リスク** (ATR x {ATR_TRAIL_MULTIPLIER:.1f}) |\n"
        f"| 🟢 TP 目標 | <code>${format_price_utility(tp_price, symbol)}</code> | **動的決済** (DTSにより利益最大化) |\n" 
        f"| ❌ SL 位置 | <code>${format_price_utility(sl_price, symbol)}</code> | 損切 ({sl_source_str} / **初期追跡ストップ**) |\n"
        f"----------------------------------\n"
    )
    
    # ----------------------------------------------------
    # 🚨 NEW SECTION: 損益計算テーブルの追加
    # ----------------------------------------------------
    pnl_table = ""
    
    if side == 'ロング':
        # ロングの場合、R1/R2がTPターゲット、S1/S2が損切目標または一時的な下落目標
        pnl_table = (
            f"**🧮 ${POSITION_CAPITAL:,.0f} ポジションの到達損益 (Pivot S/R) [1x想定]**\n"
            f"----------------------------------\n"
            f"| 目標レベル | **価格 (USD)** | 損益 (概算) |\n"
            f"| :--- | :--- | :--- |\n"
            f"| 📈 **抵抗線 R1 (TP)** | <code>${format_price_utility(pivot_r1, symbol)}</code> | {format_pnl_utility_telegram(pnl_r1)} |\n"
            f"| 🚀 **抵抗線 R2 (TP)** | <code>${format_price_utility(pivot_r2, symbol)}</code> | {format_pnl_utility_telegram(pnl_r2)} |\n"
            f"| 📉 **支持線 S1 (SL)** | <code>${format_price_utility(pivot_s1, symbol)}</code> | {format_pnl_utility_telegram(pnl_s1)} |\n"
            f"| 🚨 **支持線 S2 (SL)** | <code>${format_price_utility(pivot_s2, symbol)}</code> | {format_pnl_utility_telegram(pnl_s2)} |\n"
            f"----------------------------------\n"
        )
    elif side == 'ショート':
        # ショートの場合、S1/S2がTPターゲット、R1/R2が損切目標または一時的な上昇目標
        pnl_table = (
            f"**🧮 ${POSITION_CAPITAL:,.0f} ポジションの到達損益 (Pivot S/R) [1x想定]**\n"
            f"----------------------------------\n"
            f"| 目標レベル | **価格 (USD)** | 損益 (概算) |\n"
            f"| :--- | :--- | :--- |\n"
            f"| 📈 **抵抗線 R1 (SL)** | <code>${format_price_utility(pivot_r1, symbol)}</code> | {format_pnl_utility_telegram(pnl_r1)} |\n"
            f"| 🚨 **抵抗線 R2 (SL)** | <code>${format_price_utility(pivot_r2, symbol)}</code> | {format_pnl_utility_telegram(pnl_r2)} |\n"
            f"| 📉 **支持線 S1 (TP)** | <code>${format_price_utility(pivot_s1, symbol)}</code> | {format_pnl_utility_telegram(pnl_s1)} |\n"
            f"| 🚀 **支持線 S2 (TP)** | <code>${format_price_utility(pivot_s2, symbol)}</code> | {format_pnl_utility_telegram(pnl_s2)} |\n"
            f"----------------------------------\n"
        )
    # ----------------------------------------------------
        
    # ----------------------------------------------------
    # 2. 統合分析サマリーとスコアリングの詳細
    # ----------------------------------------------------
    analysis_detail = "**💡 統合シグナル生成の根拠 (3時間軸)**\n"
    
    long_term_trend_4h = 'Neutral'
    
    for s in signals:
        tf = s.get('timeframe')
        s_side = s.get('side', 'N/A')
        s_score = s.get('score', 0.5)
        tech_data_s = s.get('tech_data', {}) # s: シグナルごとのデータ
        
        score_in_100 = s_score * 100
        
        if tf == '4h':
            long_term_trend_4h = tech_data_s.get('long_term_trend', 'Neutral')
            analysis_detail += (
                f"🌏 **4h 足** (長期トレンド): **{long_term_trend_4h}** ({score_in_100:.2f}点)\n"
            )
            
        else:
            score_icon = "🔥" if s_score >= 0.75 else ("📈" if s_score >= 0.65 else "🟡" )
            
            penalty_status = f" (逆張りペナルティ: -{tech_data_s.get('long_term_reversal_penalty_value', 0.0) * 100:.1f}点適用)" if tech_data_s.get('long_term_reversal_penalty') else ""
            
            momentum_valid = tech_data_s.get('macd_cross_valid', True)
            momentum_text = "[✅ モメンタム確証: OK]" if momentum_valid else f"[⚠️ モメンタム反転により減点: -{tech_data_s.get('macd_cross_penalty_value', 0.0) * 100:.1f}点]"

            vwap_consistent = tech_data_s.get('vwap_consistent', False)
            vwap_text = "[🌊 VWAP一致: OK]" if vwap_consistent else "[🌊 VWAP不一致: NG]"

            stoch_penalty = tech_data_s.get('stoch_filter_penalty', 0.0)
            stoch_text = ""
            if stoch_penalty > 0:
                 stoch_text = f" [⚠️ STOCHRSI 過熱感により減点: -{stoch_penalty * 100:.2f}点]"
            elif stoch_penalty == 0 and tf in ['15m', '1h']:
                 stoch_text = f" [✅ STOCHRSI 確証]"

            analysis_detail += (
                f"**[{tf} 足] {score_icon}** ({score_in_100:.2f}点) -> **{s_side}**{penalty_status} {momentum_text} {vwap_text} {stoch_text}\n"
            )
            
            # 採用された時間軸の技術指標を詳細に表示 (best_signalのtech_dataを使用)
            if tf == timeframe:
                regime = best_signal.get('regime', 'N/A')
                # ADX/Regime
                analysis_detail += f"   └ **ADX/Regime**: {tech_data.get('adx', 0.0):.2f} ({regime})\n"
                # RSI/MACDH/CCI/STOCH
                analysis_detail += f"   └ **RSI/MACDH/CCI**: {tech_data.get('rsi', 0.0):.2f} / {tech_data.get('macd_hist', 0.0):.4f} / {tech_data.get('cci', 0.0):.2f}\n"

                # Structural/Pivot Analysis
                pivot_bonus = tech_data.get('structural_pivot_bonus', 0.0)
                pivot_status = "✅ 構造的S/R確証" if pivot_bonus > 0 else "❌ 構造確証なし"
                analysis_detail += f"   └ **構造分析(Pivot)**: {pivot_status} (+{pivot_bonus * 100:.2f}点)\n"

                # 出来高確証の表示
                volume_bonus = tech_data.get('volume_confirmation_bonus', 0.0)
                if volume_bonus > 0:
                    analysis_detail += f"   └ **出来高/流動性確証**: ✅ +{volume_bonus * 100:.2f}点 ボーナス追加 (平均比率: {tech_data.get('volume_ratio', 0.0):.1f}x)\n"
                else:
                    analysis_detail += f"   └ **出来高/流動性確証**: ❌ 確認なし (比率: {tech_data.get('volume_ratio', 0.0):.1f}x)\n"
                
                # Funding Rate Analysis
                funding_rate_val = tech_data.get('funding_rate_value', 0.0)
                funding_rate_bonus = tech_data.get('funding_rate_bonus_value', 0.0)
                funding_rate_status = ""
                if funding_rate_bonus > 0:
                    funding_rate_status = f"✅ 優位性あり (+{funding_rate_bonus * 100:.2f}点)"
                elif funding_rate_bonus < 0:
                    funding_rate_status = f"⚠️ 過密ペナルティ適用 (-{abs(funding_rate_bonus) * 100:.2f}点)"
                else:
                    funding_rate_status = "❌ フィルター範囲外"
                
                analysis_detail += f"   └ **資金調達率 (FR)**: {funding_rate_val * 100:.4f}% (8h) - {funding_rate_status}\n"

                # Dominance Analysis
                dominance_trend = tech_data.get('dominance_trend', 'Neutral')
                dominance_bonus = tech_data.get('dominance_bias_bonus_value', 0.0)
                
                dominance_status = ""
                if dominance_bonus > 0:
                    dominance_status = f"✅ 優位性あり (+{dominance_bonus * 100:.2f}点)"
                elif dominance_bonus < 0:
                    dominance_status = f"⚠️ バイアスにより減点適用 (-{abs(dominance_bonus) * 100:.2f}点)"
                else:
                    dominance_status = "❌ フィルター範囲外/非該当"
                
                trend_display = ""
                if symbol != 'BTC-USDT':
                     trend_display = f" (Altcoin Bias: {dominance_trend})"
                
                analysis_detail += f"   └ **BTCドミナンス**: {dominance_trend} トレンド{trend_display} - {dominance_status}\n"


    # 3. リスク管理とフッター
    regime = best_signal.get('regime', 'N/A')
    
    footer = (
        f"==================================\n"
        f"| 🔍 **市場環境** | **{regime}** 相場 (ADX: {best_signal.get('tech_data', {}).get('adx', 0.0):.2f}) |\n"
        f"| ⚙️ **BOT Ver** | **v16.0.4** - ValueError Fix |\n" # バージョンを v16.0.4 に更新
        f"==================================\n"
        f"\n<pre>※ Limit注文は、価格が指定水準に到達した際のみ約定します。DTS戦略では、価格が有利な方向に動いた場合、SLが自動的に追跡され利益を最大化します。</pre>"
    )

    return header + trade_plan + pnl_table + analysis_detail + footer


# ====================================================================================
# CCXT & DATA ACQUISITION
# ====================================================================================

async def initialize_ccxt_client():
    """CCXTクライアントを初期化 (OKX)"""
    global EXCHANGE_CLIENT
    
    EXCHANGE_CLIENT = ccxt_async.okx({
        'timeout': 30000, 
        'enableRateLimit': True,
        'options': {'defaultType': 'swap'} 
    })
    
    if EXCHANGE_CLIENT:
        logging.info(f"CCXTクライアントを初期化しました ({CCXT_CLIENT_NAME} - リアル接続, Default: Swap)")
    else:
        logging.error("CCXTクライアントの初期化に失敗しました。")


def convert_symbol_to_okx_swap(symbol: str) -> str:
    """USDT現物シンボル (BTC/USDT) をOKXの無期限スワップシンボル (BTC-USDT) に変換する"""
    return symbol.replace('/', '-')

async def fetch_funding_rate(symbol: str) -> float:
    """OKXからシンボルの直近の資金調達率を取得する"""
    global EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT:
        return 0.0
    try:
        funding_rate = await EXCHANGE_CLIENT.fetch_funding_rate(symbol)
        return funding_rate.get('fundingRate', 0.0) if funding_rate else 0.0
    except Exception as e:
        return 0.0

async def update_symbols_by_volume():
    """CCXTを使用してOKXの出来高トップ30のUSDTペア銘柄を動的に取得・更新する"""
    global CURRENT_MONITOR_SYMBOLS, EXCHANGE_CLIENT, LAST_SUCCESSFUL_MONITOR_SYMBOLS
    
    if not EXCHANGE_CLIENT:
        return

    try:
        tickers_spot = await EXCHANGE_CLIENT.fetch_tickers(params={'instType': 'SPOT'})
        
        usdt_tickers = {
            symbol: ticker for symbol, ticker in tickers_spot.items() 
            if symbol.endswith('/USDT') and ticker.get('quoteVolume') is not None
        }

        sorted_tickers = sorted(
            usdt_tickers.items(), 
            key=lambda item: item[1]['quoteVolume'], 
            reverse=True
        )
        
        new_monitor_symbols = [convert_symbol_to_okx_swap(symbol) for symbol, _ in sorted_tickers[:TOP_SYMBOL_LIMIT]]
        
        if new_monitor_symbols:
            CURRENT_MONITOR_SYMBOLS = new_monitor_symbols
            LAST_SUCCESSFUL_MONITOR_SYMBOLS = new_monitor_symbols.copy()
        else:
            CURRENT_MONITOR_SYMBOLS = LAST_SUCCESSFUL_MONITOR_SYMBOLS
            logging.warning("⚠️ 出来高データを取得できませんでした。前回成功したリストを使用します。")

    except Exception as e:
        logging.error(f"出来高による銘柄更新中にエラーが発生しました: {e}")
        CURRENT_MONITOR_SYMBOLS = LAST_SUCCESSFUL_MONITOR_SYMBOLS
        logging.warning("⚠️ 出来高データ取得エラー。前回成功したリストにフォールバックします。")

        
async def fetch_ohlcv_with_fallback(client_name: str, symbol: str, timeframe: str) -> Tuple[List[List[float]], str, str]:
    """CCXTを使用してOHLCVデータを取得し、エラー発生時にフォールバックする"""
    global EXCHANGE_CLIENT

    if not EXCHANGE_CLIENT:
        return [], "ExchangeError", client_name

    try:
        limit = REQUIRED_OHLCV_LIMITS.get(timeframe, 100)
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=limit, params={'instType': 'SWAP'})
        
        if not ohlcv or len(ohlcv) < 30:
            return [], "DataShortage", client_name
        
        return ohlcv, "Success", client_name
    
    except ccxt.NetworkError as e:
        return [], "ExchangeError", client_name
    
    except ccxt.ExchangeError as e:
        # スワップが見つからない場合、現物 (SPOT) を試みる
        if 'market symbol' in str(e) or 'not found' in str(e):
            spot_symbol = symbol.replace('-', '/')
            try:
                ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(spot_symbol, timeframe, limit=limit, params={'instType': 'SPOT'})
                if not ohlcv or len(ohlcv) < 30:
                    return [], "DataShortage", client_name
                return ohlcv, "Success", client_name
            except Exception:
                return [], "ExchangeError", client_name
        
        return [], "ExchangeError", client_name
        
    except Exception as e:
        # その他のエラー
        return [], "ExchangeError", client_name

async def get_crypto_macro_context() -> Dict:
    """
    マクロ市場コンテキストを取得 (FGI Proxy, BTC/ETH Trend, Dominance Bias)
    """
    
    # 1. BTC/USDTとETH/USDTの長期トレンドと直近の価格変化率を取得 (4h足)
    btc_ohlcv, status_btc, _ = await fetch_ohlcv_with_fallback(CCXT_CLIENT_NAME, "BTC-USDT", '4h')
    eth_ohlcv, status_eth, _ = await fetch_ohlcv_with_fallback(CCXT_CLIENT_NAME, "ETH-USDT", '4h')
    
    btc_trend = 0
    eth_trend = 0
    btc_change = 0.0
    eth_change = 0.0
    
    # 🚨 FIX: btc_dom_changeを事前に初期化 (ValueError/UnboundLocalError対策)
    btc_dom_change = 0.0
    
    df_btc = pd.DataFrame()
    df_eth = pd.DataFrame()
    
    if status_btc == "Success":
        df_btc = pd.DataFrame(btc_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df_btc['close'] = pd.to_numeric(df_btc['close'], errors='coerce').astype('float64')
        df_btc['sma'] = ta.sma(df_btc['close'], length=LONG_TERM_SMA_LENGTH)
        df_btc.dropna(subset=['sma'], inplace=True)
        
        if not df_btc.empty:
            if df_btc['close'].iloc[-1] > df_btc['sma'].iloc[-1]:
                btc_trend = 1 # Long
            elif df_btc['close'].iloc[-1] < df_btc['sma'].iloc[-1]:
                btc_trend = -1 # Short
            
            if len(df_btc) >= 2:
                # 直近の価格変化率 (4hの終値ベース)
                btc_change = (df_btc['close'].iloc[-1] - df_btc['close'].iloc[-2]) / df_btc['close'].iloc[-2]

    if status_eth == "Success":
        df_eth = pd.DataFrame(eth_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df_eth['close'] = pd.to_numeric(df_eth['close'], errors='coerce').astype('float64')
        df_eth['sma'] = ta.sma(df_eth['close'], length=LONG_TERM_SMA_LENGTH)
        df_eth.dropna(subset=['sma'], inplace=True)
        
        if not df_eth.empty:
            if df_eth['close'].iloc[-1] > df_eth['sma'].iloc[-1]:
                eth_trend = 1 # Long
            elif df_eth['close'].iloc[-1] < df_eth['sma'].iloc[-1]:
                eth_trend = -1 # Short

    # 2. BTC Dominanceのトレンドを取得
    # yfinance (Yahoo Finance) を使用してBTC.Dの代理としてBTC/USDの直近のボラティリティ/モメンタムを測定
    try:
        btc_dom = yf.download(tickers="BTC-USD", period="1d", interval="1h", progress=False)
        
        # 🚨 FIX: NaNチェックとゼロ除算防止を追加
        if not btc_dom.empty and len(btc_dom) >= 2:
            close_latest = btc_dom['Close'].iloc[-1]
            close_previous = btc_dom['Close'].iloc[-2]
            
            # NaNチェックとゼロ除算チェック
            if pd.notna(close_latest) and pd.notna(close_previous) and close_previous != 0:
                # 1時間の価格変化をドミナンス変化のプロキシとして使用 (非常に単純化されたFGIプロキシ)
                btc_dom_change = (close_latest - close_previous) / close_previous
            else:
                 btc_dom_change = 0.0 # 計算不能な場合は0.0に設定
            
    except Exception as e:
        # 🚨 FIX: エラー時にも btc_dom_change が必ず設定されるようにする
        logging.warning(f"Yahoo FinanceからのBTCデータ取得エラー: {e}. btc_dom_changeを 0.0 に設定します。")
        btc_dom_change = 0.0

    # 3. マクロコンテキストの統合
    # Fear & Greed Index (FGI) Proxy: BTCの短期的なボラティリティとトレンドの組み合わせ
    # ドミナンス変化が正で上昇トレンドならリスクオン (Altcoinにペナルティ)、負で下降トレンドならリスクオフ (Altcoinにボーナス)
    sentiment_fgi_proxy = (btc_trend * 0.5) + (btc_change * 10) 
    
    # Dominance Bias Trend: btc_dom_changeの方向
    dominance_trend = 'Neutral'
    if btc_dom_change > 0.005: # BTCが急騰（リスクオフ傾向/Altcoinペナルティ）
        dominance_trend = 'StrongUp' 
    elif btc_dom_change > 0.001:
        dominance_trend = 'Up'
    elif btc_dom_change < -0.005: # BTCが急落（リスクオン傾向/Altcoinボーナス）
        dominance_trend = 'StrongDown'
    elif btc_dom_change < -0.001:
        dominance_trend = 'Down'
    
    return {
        'btc_trend': btc_trend,
        'eth_trend': eth_trend,
        'btc_change': btc_change,
        'eth_change': eth_change,
        'dominance_trend': dominance_trend,
        'dominance_change': btc_dom_change,
        'sentiment_fgi_proxy': sentiment_fgi_proxy
    }


# ====================================================================================
# TRADING SIGNAL CORE LOGIC
# ====================================================================================

async def analyze_and_generate_signal(
    symbol: str, 
    timeframe: str, 
    macro_context: Dict, 
    ohlcv_15m: Optional[List[List[float]]] = None
) -> Dict: 
    """
    単一銘柄・単一時間軸の取引シグナルとテクニカル指標を生成する
    """
    
    # ----------------------------------------------------------------------
    # 1. データ取得
    # ----------------------------------------------------------------------
    ohlcv, status, client_name = await fetch_ohlcv_with_fallback(CCXT_CLIENT_NAME, symbol, timeframe) 

    if status != "Success":
        return {
            'symbol': symbol, 
            'timeframe': timeframe, 
            'side': status, 
            'score': 0.0, 
            'tech_data': {},
            'macro_context': macro_context
        }

    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['close'] = pd.to_numeric(df['close'], errors='coerce').astype('float64')
    df['high'] = pd.to_numeric(df['high'], errors='coerce').astype('float64')
    df['low'] = pd.to_numeric(df['low'], errors='coerce').astype('float64')
    df['volume'] = pd.to_numeric(df['volume'], errors='coerce').astype('float64')
    df.set_index(pd.to_datetime(df['timestamp'], unit='ms', utc=True), inplace=True)
    
    # 出来高が極端に低い (TOP 10% 以下) 場合のデータ不足とみなす
    if df['volume'].iloc[-1] < df['volume'].quantile(0.1):
         # このケースは一旦無視し、出来高のペナルティで対応する
         pass 

    # 最後の終値を取得
    current_price = df['close'].iloc[-1]

    # ----------------------------------------------------------------------
    # 2. テクニカル指標の計算
    # ----------------------------------------------------------------------
    df.ta.ema(close='close', length=20, append=True)
    df.ta.rsi(length=14, append=True)
    df.ta.macd(fast=12, slow=26, signal=9, append=True)
    df.ta.bbands(length=20, std=2, append=True)
    df.ta.adx(length=14, append=True)
    df.ta.cci(length=20, append=True)
    df.ta.stoch(k=14, d=3, smooth_k=3, append=True)
    df.ta.sma(close='close', length=LONG_TERM_SMA_LENGTH, append=True)
    df.ta.atr(length=14, append=True)
    
    # Pivot Point (構造的SL計算用 - Fibonacci)
    pivots = ta.pivots(df['high'], df['low'], df['close'], kind='fibonacci')
    df = pd.concat([df, pivots], axis=1)

    # VWAP (15m足のOHLCVが提供されている場合のみ計算可能)
    vwap_consistent = False
    if ohlcv_15m and timeframe == '1h':
        df_15m = pd.DataFrame(ohlcv_15m, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df_15m['close'] = pd.to_numeric(df_15m['close'], errors='coerce').astype('float64')
        df_15m['volume'] = pd.to_numeric(df_15m['volume'], errors='coerce').astype('float64')
        df_15m.ta.vwap(append=True)
        if not df_15m.empty and 'VWAP' in df_15m.columns:
            vwap_current = df_15m['VWAP'].iloc[-1]
            # VWAPとの関係をチェック
            if current_price > vwap_current:
                 vwap_consistent = True 
            else:
                 vwap_consistent = False 

    # ----------------------------------------------------------------------
    # 3. シグナルスコアリング
    # ----------------------------------------------------------------------
    
    score = BASE_SCORE
    side = 'Neutral'
    structural_sl_used = False 
    structural_pivot_bonus = 0.0
    long_term_reversal_penalty = False
    macd_cross_valid = True
    volume_confirmation_bonus = 0.0
    funding_rate_bonus_value = 0.0
    dominance_bias_bonus_value = 0.0
    stoch_filter_penalty = 0.0
    
    # 最終的な指標値
    rsi_val = df['RSI_14'].iloc[-1]
    macd_hist_val = df['MACDH_12_26_9'].iloc[-1]
    adx_val = df['ADX_14'].iloc[-1]
    cci_val = df['CCI_20'].iloc[-1]
    atr_val = df['ATR_14'].iloc[-1]
    stoch_k = df['STOCHk_14_3_3'].iloc[-1]
    stoch_d = df['STOCHd_14_3_3'].iloc[-1]
    sma_long = df[f'SMA_{LONG_TERM_SMA_LENGTH}'].iloc[-1]
    
    # Pivot Point値
    pivot_r1_val = df['R1'].iloc[-1]
    pivot_r2_val = df['R2'].iloc[-1]
    pivot_s1_val = df['S1'].iloc[-1]
    pivot_s2_val = df['S2'].iloc[-1]
    
    # ATRに基づく初期SL幅
    sl_width = atr_val * ATR_TRAIL_MULTIPLIER
    entry_price = current_price
    entry_type = 'Market (Fallback)' # デフォルト

    # ----------------------------------------------------------------------
    # 3.1. トレンド・モメンタム判定 (ADX/MACD)
    # ----------------------------------------------------------------------
    is_trending = adx_val >= ADX_TREND_THRESHOLD
    regime = 'Trend' if is_trending else 'Consolidation'

    # ----------------------------------------------------------------------
    # 3.2. ロングシグナル判定
    # ----------------------------------------------------------------------
    if (rsi_val < RSI_OVERSOLD and cci_val < -100 and macd_hist_val > 0) or \
       (rsi_val < RSI_MOMENTUM_LOW and macd_hist_val > 0.0): # 積極的なエントリー条件

        side = 'ロング'
        score_multiplier = 1.0

        # RSI/CCI/MACDベースのスコアリング
        score += (RSI_OVERSOLD - rsi_val) / (RSI_OVERSOLD - 20) * 0.15 # RSI買われすぎ補正
        score += max(0.0, -cci_val / 100 * 0.15)
        score += min(0.15, max(0.0, macd_hist_val * 0.05)) # モメンタム

        # 出来高の確認 (過去20本の平均出来高に対する直近出来高の倍率)
        volume_ratio = df['volume'].iloc[-1] / df['volume'].rolling(20).mean().iloc[-2]
        if volume_ratio >= VOLUME_CONFIRMATION_MULTIPLIER:
            volume_confirmation_bonus = 0.10 
            score += volume_confirmation_bonus

        # 構造的サポート（Pivot S1, S2, S3）に基づくボーナス
        pivot_s1 = df['S1'].iloc[-1]
        pivot_s2 = df['S2'].iloc[-1]
        
        if current_price <= pivot_s1 * 1.001:
            structural_pivot_bonus = 0.15 # サポート付近
            score += structural_pivot_bonus
            
            # SLを構造的サポート（S2またはS1の下）に設定
            sl_price = min(current_price - sl_width, pivot_s2)
            # FIX: SLにATRバッファを追加 (エントリーとの一致を避ける)
            sl_price = sl_price - (atr_val * 0.5) 
            
            structural_sl_used = True
        else:
            sl_price = current_price - sl_width # ATRベースのSL
        
        entry_price = current_price - atr_val * 0.25 # Limit注文を現在の価格より少し下に置く
        entry_type = 'Limit (Deep)' if current_price - entry_price > 0.5 * atr_val else 'Limit (Close)'
        
        # MACDが下向き (モメンタム反転) の場合、ペナルティ
        if macd_hist_val < 0.0: 
            macd_cross_valid = False
            score -= MACD_CROSS_PENALTY
        
        # 長期SMAを下回っている場合、逆張りペナルティ (4h足はトレンド判定のみに使用)
        if timeframe != '4h' and current_price < sma_long:
            long_term_reversal_penalty = True
            score -= LONG_TERM_REVERSAL_PENALTY

        # STOCHRSIの過熱感フィルタ (KとDが80以上でペナルティ)
        if stoch_k > 80.0 and stoch_d > 80.0:
            stoch_filter_penalty = 0.10
            score -= stoch_filter_penalty
            
        # 資金調達率フィルタ (ロングでFRがプラス過密の場合、ペナルティ)
        funding_rate = await fetch_funding_rate(symbol)
        if funding_rate > FUNDING_RATE_THRESHOLD:
            funding_rate_bonus_value = -FUNDING_RATE_BONUS_PENALTY
            score += funding_rate_bonus_value
        elif funding_rate < -FUNDING_RATE_THRESHOLD:
            funding_rate_bonus_value = FUNDING_RATE_BONUS_PENALTY
            score += funding_rate_bonus_value
            
        # ドミナンスバイアスフィルタ (BTCが強い上昇トレンド=リスクオフ=Altcoinペナルティ)
        if symbol != 'BTC-USDT':
             if macro_context.get('dominance_trend') in ['Up', 'StrongUp']:
                 dominance_bias_bonus_value = -DOMINANCE_BIAS_BONUS_PENALTY
                 score += dominance_bias_bonus_value
             elif macro_context.get('dominance_trend') in ['Down', 'StrongDown']:
                 dominance_bias_bonus_value = DOMINANCE_BIAS_BONUS_PENALTY
                 score += dominance_bias_bonus_value

    # ----------------------------------------------------------------------
    # 3.3. ショートシグナル判定
    # ----------------------------------------------------------------------
    elif (rsi_val > RSI_OVERBOUGHT and cci_val > 100 and macd_hist_val < 0) or \
         (rsi_val > RSI_MOMENTUM_HIGH and macd_hist_val < 0.0): # 積極的なエントリー条件

        side = 'ショート'
        score_multiplier = 1.0
        
        # RSI/CCI/MACDベースのスコアリング
        score += (rsi_val - RSI_OVERBOUGHT) / (100 - RSI_OVERBOUGHT) * 0.15 # RSI売られすぎ補正
        score += max(0.0, cci_val / 100 * 0.15)
        score += min(0.15, max(0.0, -macd_hist_val * 0.05)) # モメンタム

        # 出来高の確認
        volume_ratio = df['volume'].iloc[-1] / df['volume'].rolling(20).mean().iloc[-2]
        if volume_ratio >= VOLUME_CONFIRMATION_MULTIPLIER:
            volume_confirmation_bonus = 0.10
            score += volume_confirmation_bonus
            
        # 構造的レジスタンス（Pivot R1, R2, R3）に基づくボーナス
        pivot_r1 = df['R1'].iloc[-1]
        pivot_r2 = df['R2'].iloc[-1]
        
        if current_price >= pivot_r1 * 0.999:
            structural_pivot_bonus = 0.15 # レジスタンス付近
            score += structural_pivot_bonus
            
            # SLを構造的レジスタンス（R2またはR1の上）に設定
            sl_price = max(current_price + sl_width, pivot_r2)
            # FIX: SLにATRバッファを追加 (エントリーとの一致を避ける)
            sl_price = sl_price + (atr_val * 0.5) 

            structural_sl_used = True
        else:
            sl_price = current_price + sl_width # ATRベースのSL

        entry_price = current_price + atr_val * 0.25 # Limit注文を現在の価格より少し上に置く
        entry_type = 'Limit (Deep)' if entry_price - current_price > 0.5 * atr_val else 'Limit (Close)'
        
        # MACDが上向き (モメンタム反転) の場合、ペナルティ
        if macd_hist_val > 0.0:
            macd_cross_valid = False
            score -= MACD_CROSS_PENALTY
            
        # 長期SMAを上回っている場合、逆張りペナルティ (4h足はトレンド判定のみに使用)
        if timeframe != '4h' and current_price > sma_long:
            long_term_reversal_penalty = True
            score -= LONG_TERM_REVERSAL_PENALTY

        # STOCHRSIの過熱感フィルタ (KとDが20以下でペナルティ)
        if stoch_k < 20.0 and stoch_d < 20.0:
            stoch_filter_penalty = 0.10
            score -= stoch_filter_penalty
            
        # 資金調達率フィルタ (ショートでFRがマイナス過密の場合、ペナルティ)
        funding_rate = await fetch_funding_rate(symbol)
        if funding_rate < -FUNDING_RATE_THRESHOLD:
            funding_rate_bonus_value = -FUNDING_RATE_BONUS_PENALTY
            score += funding_rate_bonus_value
        elif funding_rate > FUNDING_RATE_THRESHOLD:
            funding_rate_bonus_value = FUNDING_RATE_BONUS_PENALTY
            score += funding_rate_bonus_value
            
        # ドミナンスバイアスフィルタ (BTCが強い下落トレンド=リスクオン=Altcoinボーナス)
        if symbol != 'BTC-USDT':
             if macro_context.get('dominance_trend') in ['Down', 'StrongDown']:
                 dominance_bias_bonus_value = DOMINANCE_BIAS_BONUS_PENALTY
                 score += dominance_bias_bonus_value
             elif macro_context.get('dominance_trend') in ['Up', 'StrongUp']:
                 dominance_bias_bonus_value = -DOMINANCE_BIAS_BONUS_PENALTY
                 score += dominance_bias_bonus_value
                 
    # 4. スコアのクリッピングとTP/RRRの計算
    score = max(0.0, min(1.0, score))
    
    # RRR (Target Profit / Risk) の計算
    tp_price = 0.0 
    rr_ratio = 0.0
    
    # リスク幅 (エントリーとSLの差)
    risk_width = abs(entry_price - sl_price) if side != 'Neutral' else 0.000001
    
    if risk_width > 0.0 and side != 'Neutral':
        # TPはDTSのターゲットとして、高めのRRR (DTS_RRR_DISPLAY) を設定
        if side == 'ロング':
            tp_price = entry_price + risk_width * DTS_RRR_DISPLAY
        elif side == 'ショート':
            tp_price = entry_price - risk_width * DTS_RRR_DISPLAY
            
        rr_ratio = DTS_RRR_DISPLAY
    
    # 5. 結果の返却
    return {
        'symbol': symbol, 
        'timeframe': timeframe, 
        'side': side, 
        'score': score, 
        'rr_ratio': rr_ratio,
        'price': current_price,
        'entry': entry_price,
        'tp1': tp_price,
        'sl': sl_price,
        'entry_type': entry_type,
        'regime': regime,
        'macro_context': macro_context,
        'tech_data': {
            'rsi': rsi_val,
            'macd_hist': macd_hist_val,
            'adx': adx_val,
            'cci': cci_val,
            'atr_value': atr_val,
            'long_term_trend': 'Long' if current_price > sma_long else 'Short' if current_price < sma_long else 'Neutral',
            'long_term_reversal_penalty': long_term_reversal_penalty,
            'long_term_reversal_penalty_value': LONG_TERM_REVERSAL_PENALTY if long_term_reversal_penalty else 0.0,
            'macd_cross_valid': macd_cross_valid,
            'macd_cross_penalty_value': MACD_CROSS_PENALTY if not macd_cross_valid else 0.0,
            'structural_sl_used': structural_sl_used,
            'structural_pivot_bonus': structural_pivot_bonus,
            'volume_confirmation_bonus': volume_confirmation_bonus,
            'volume_ratio': volume_ratio,
            'funding_rate_value': funding_rate if side in ['ロング', 'ショート'] else 0.0,
            'funding_rate_bonus_value': funding_rate_bonus_value,
            'dominance_trend': macro_context.get('dominance_trend', 'N/A'),
            'dominance_bias_bonus_value': dominance_bias_bonus_value,
            'stoch_filter_penalty': stoch_filter_penalty,
            'vwap_consistent': vwap_consistent,
            # 🚨 ADDED: Pivot Point Values for P/L Calculation
            'pivot_r1': pivot_r1_val,
            'pivot_r2': pivot_r2_val,
            'pivot_s1': pivot_s1_val,
            'pivot_s2': pivot_s2_val,
        }
    }


# ====================================================================================
# MAIN LOOP
# ====================================================================================

async def main_loop():
    """
    メインの監視・シグナル生成ループ
    """
    global LAST_UPDATE_TIME, LAST_ANALYSIS_SIGNALS, LAST_SUCCESS_TIME, GLOBAL_MACRO_CONTEXT
    
    # 起動時のクライアント初期化
    await initialize_ccxt_client()

    while True:
        try:
            current_time = time.time()
            
            # 出来高トップ銘柄の更新 (一定間隔)
            if current_time - LAST_UPDATE_TIME > 60 * 60 * 4 or not CURRENT_MONITOR_SYMBOLS:
                 logging.info("📝 出来高トップ銘柄リストを更新します。")
                 await update_symbols_by_volume()
                 LAST_UPDATE_TIME = current_time
                 logging.info(f"✅ 監視銘柄数: {len(CURRENT_MONITOR_SYMBOLS)}")

            # マクロコンテキストの更新
            logging.info("🌎 マクロ市場コンテキスト (BTCトレンド/ドミナンス) を取得します。")
            GLOBAL_MACRO_CONTEXT = await get_crypto_macro_context()
            logging.info(f"✅ マクロコンテキスト更新完了: FGI Proxy={GLOBAL_MACRO_CONTEXT.get('sentiment_fgi_proxy', 0.0):.2f}, Dominance Trend={GLOBAL_MACRO_CONTEXT.get('dominance_trend', 'N/A')}")

            tasks = []
            all_signals = []
            
            # 銘柄ごとの処理を非同期で実行
            for symbol in CURRENT_MONITOR_SYMBOLS:
                # 15分足のデータは他の時間軸のVWAP計算に使用するため、事前に取得を試みる
                ohlcv_15m, status_15m, _ = await fetch_ohlcv_with_fallback(CCXT_CLIENT_NAME, symbol, '15m')
                
                for timeframe in ['15m', '1h', '4h']:
                    # 15m足のデータがある場合はそれをanalyze_and_generate_signalに渡す
                    current_ohlcv_15m = ohlcv_15m if timeframe in ['1h', '4h'] and status_15m == 'Success' else None
                    
                    task = analyze_and_generate_signal(
                        symbol, 
                        timeframe, 
                        GLOBAL_MACRO_CONTEXT,
                        current_ohlcv_15m
                    )
                    tasks.append(task)
                    
                    # レート制限を考慮した遅延
                    await asyncio.sleep(REQUEST_DELAY_PER_SYMBOL) 
            
            # すべての分析タスクの完了を待つ
            logging.info(f"🔍 全 {len(CURRENT_MONITOR_SYMBOLS)} 銘柄、全 3 時間軸 ({len(tasks)} 件) の分析を開始します...")
            analysis_results = await asyncio.gather(*tasks)
            logging.info("✅ 全銘柄の分析が完了しました。")

            # シグナルの統合とフィルタリング
            signal_map: Dict[str, List[Dict]] = {}
            for result in analysis_results:
                 sym = result['symbol']
                 if sym not in signal_map:
                     signal_map[sym] = []
                 signal_map[sym].append(result)

            # 統合されたシグナルのフィルタリングとランキング
            final_signals: List[Dict] = []
            for symbol, signals in signal_map.items():
                valid_signals = [s for s in signals if s.get('side') not in ["DataShortage", "ExchangeError", "Neutral"] and s.get('score', 0.0) >= SIGNAL_THRESHOLD]
                
                if not valid_signals:
                    continue
                    
                # 最もスコアが高いシグナルをその銘柄の代表シグナルとする
                best_signal = max(
                    valid_signals, 
                    key=lambda s: (s.get('score', 0.5), s.get('rr_ratio', 0.0), s.get('tech_data', {}).get('adx', 0.0))
                )
                
                # クールダウンチェック
                if current_time - TRADE_NOTIFIED_SYMBOLS.get(symbol, 0.0) > TRADE_SIGNAL_COOLDOWN:
                    final_signals.append(best_signal)
                else:
                    logging.info(f"⏱️ {symbol} はクールダウン期間中です。スキップします。")
            
            # スコア順にソートし、TOP_SIGNAL_COUNTに絞る
            final_signals.sort(key=lambda s: s['score'], reverse=True)
            
            LAST_ANALYSIS_SIGNALS = final_signals[:TOP_SIGNAL_COUNT]
            
            # Telegram通知
            if LAST_ANALYSIS_SIGNALS:
                logging.warning(f"🔔 高スコアシグナル {len(LAST_ANALYSIS_SIGNALS)} 件が見つかりました！通知します。")
                
                for i, signal in enumerate(LAST_ANALYSIS_SIGNALS):
                    rank = i + 1
                    symbol = signal['symbol']
                    # format_integrated_analysis_messageがv16.0.4のフォーマット（Pivot価格入り）でメッセージを生成
                    message = format_integrated_analysis_message(symbol, signal_map[symbol], rank)
                    
                    send_telegram_html(message)
                    
                    # 通知後、クールダウン状態に設定
                    TRADE_NOTIFIED_SYMBOLS[symbol] = current_time
                    await asyncio.sleep(1) # 連投防止の遅延

            else:
                logging.info("❌ 閾値を超えるシグナルは見つかりませんでした。")

            LAST_SUCCESS_TIME = current_time
            
            # メインループのインターバル待機
            await asyncio.sleep(LOOP_INTERVAL)

        except Exception as e:
            error_name = type(e).__name__
            
            if error_name in ['ClientError', 'NetworkError']:
                 # ネットワーク関連のエラーはログ出力のみに留める
                 pass 
            
            logging.error(f"メインループで致命的なエラー: {error_name}")
            await asyncio.sleep(60)


# ====================================================================================
# FASTAPI SETUP
# ====================================================================================

app = FastAPI(title="Apex BOT API", version="v16.0.4 - ValueError Fix")

@app.on_event("startup")
async def startup_event():
    logging.info("🚀 Apex BOT v16.0.4 Startup initializing...") 
    asyncio.create_task(main_loop())

@app.on_event("shutdown")
async def shutdown_event():
    global EXCHANGE_CLIENT
    if EXCHANGE_CLIENT:
        await EXCHANGE_CLIENT.close()
        logging.info("CCXTクライアントをシャットダウンしました。")

@app.get("/status")
def get_status():
    status_msg = {
        "status": "ok",
        "bot_version": "v16.0.4 - ValueError Fix",
        "last_success_time_utc": datetime.fromtimestamp(LAST_SUCCESS_TIME, tz=timezone.utc).isoformat() if LAST_SUCCESS_TIME else "N/A",
        "current_client": CCXT_CLIENT_NAME,
        "monitoring_symbols": len(CURRENT_MONITOR_SYMBOLS),
        "last_signals_count": len(LAST_ANALYSIS_SIGNALS)
    }
    return JSONResponse(content=status_msg)

@app.head("/")
@app.get("/")
def home_view():
    return JSONResponse(content={"message": "Apex BOT is running on FastAPI."})
