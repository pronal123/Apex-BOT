# ====================================================================================
# Apex BOT v17.0.0 - Structural Analysis & PnL Estimate (v16.0.1 Base)
# - NEW: 抵抗候補・支持候補 (R1/S1など) の明記
# - NEW: $1000 リスクに基づいた SL/TP 損益額の表示
# - NEW: 加点/減点要素のハイライト表示
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
    adjusted_rate = 0.50 + (score - 0.50) * 1.45 
    return max(0.40, min(0.85, adjusted_rate))


def format_integrated_analysis_message(symbol: str, signals: List[Dict], rank: int) -> str:
    """
    3つの時間軸の分析結果を統合し、ログメッセージの形式に整形する (v17.0.0対応)
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
    
    # NEW: $1000 リスクに基づいたP&L計算
    sl_loss_usd = 1000.0
    # TPの予想利益は、リスクのRRR倍
    tp_gain_usd = sl_loss_usd * rr_ratio 


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
        f"| 💰 **予想損益** | **<ins>損益比 1:{rr_ratio:.2f}</ins>** (損失: ${sl_loss_usd:,.0f} / 利益: ${tp_gain_usd:,.0f}+) |\n" # NEW: P&L Display
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

    # NEW: 抵抗候補・支持候補の明記 (Feature 1)
    pivot_points = best_signal.get('tech_data', {}).get('pivot_points', {})
    
    sr_info = ""
    if pivot_points:
        r1 = format_price_utility(pivot_points.get('r1', 0.0), symbol)
        r2 = format_price_utility(pivot_points.get('r2', 0.0), symbol)
        s1 = format_price_utility(pivot_points.get('s1', 0.0), symbol)
        s2 = format_price_utility(pivot_points.get('s2', 0.0), symbol)
        pp = format_price_utility(pivot_points.get('pp', 0.0), symbol)
        
        sr_info = (
            f"\n**🧱 構造的S/R候補 (日足)**\n"
            f"----------------------------------\n"
            f"| 候補 | 価格 (USD) | 種類 |\n"
            f"| :--- | :--- | :--- |\n"
            f"| 🛡️ S2 / S1 | <code>${s2}</code> / <code>${s1}</code> | 主要な**支持 (Support)** 候補 |\n"
            f"| 🟡 PP | <code>${pp}</code> | ピボットポイント |\n"
            f"| ⚔️ R1 / R2 | <code>${r1}</code> / <code>${r2}</code> | 主要な**抵抗 (Resistance)** 候補 |\n"
            f"----------------------------------\n"
        )
    
    # ----------------------------------------------------
    # 2. 統合分析サマリーとスコアリングの詳細
    # ----------------------------------------------------
    analysis_detail = "**💡 統合シグナル生成の根拠 (3時間軸)**\n"
    
    long_term_trend_4h = 'Neutral'
    
    for s in signals:
        tf = s.get('timeframe')
        s_side = s.get('side', 'N/A')
        s_score = s.get('score', 0.5)
        tech_data = s.get('tech_data', {})
        
        score_in_100 = s_score * 100
        
        if tf == '4h':
            long_term_trend_4h = tech_data.get('long_term_trend', 'Neutral')
            analysis_detail += (
                f"🌏 **4h 足** (長期トレンド): **{long_term_trend_4h}** ({score_in_100:.2f}点)\n"
            )
            
        else:
            score_icon = "🔥" if s_score >= 0.75 else ("📈" if s_score >= 0.65 else "🟡" )
            
            # NEW: 長期トレンド逆行ペナルティのハイライト
            penalty_status = ""
            if tech_data.get('long_term_reversal_penalty'):
                 penalty_status = f" (<ins>**逆張りペナルティ**</ins>: -{tech_data.get('long_term_reversal_penalty_value', 0.0) * 100:.1f}点適用)"
            
            # NEW: MACD反転ペナルティのハイライト
            momentum_valid = tech_data.get('macd_cross_valid', True)
            momentum_text = "[✅ モメンタム確証: OK]"
            if not momentum_valid:
                 momentum_text = f"[❌ **モメンタム反転により減点** : -{tech_data.get('macd_cross_penalty_value', 0.0) * 100:.1f}点]"

            vwap_consistent = tech_data.get('vwap_consistent', False)
            vwap_text = "[🌊 VWAP一致: OK]"
            if not vwap_consistent:
                 vwap_text = "[⚠️ VWAP不一致: NG]"

            # NEW: StochRSIペナルティのハイライト
            stoch_penalty = tech_data.get('stoch_filter_penalty', 0.0)
            stoch_text = ""
            if stoch_penalty > 0:
                 stoch_text = f" [⚠️ **STOCHRSI 過熱感により減点** : -{stoch_penalty * 100:.2f}点]"
            elif stoch_penalty == 0 and tf in ['15m', '1h']:
                 stoch_text = f" [✅ STOCHRSI 確証]"

            analysis_detail += (
                f"**[{tf} 足] {score_icon}** ({score_in_100:.2f}点) -> **{s_side}**{penalty_status} {momentum_text} {vwap_text} {stoch_text}\n"
            )
            
            # 採用された時間軸の技術指標を詳細に表示
            if tf == timeframe:
                regime = best_signal.get('regime', 'N/A')
                # ADX/Regime
                analysis_detail += f"   └ **ADX/Regime**: {tech_data.get('adx', 0.0):.2f} ({regime})\n"
                # RSI/MACDH/CCI/STOCH
                analysis_detail += f"   └ **RSI/MACDH/CCI**: {tech_data.get('rsi', 0.0):.2f} / {tech_data.get('macd_hist', 0.0):.4f} / {tech_data.get('cci', 0.0):.2f}\n"

                # NEW: 構造的S/Rボーナスのハイライト
                pivot_bonus = tech_data.get('structural_pivot_bonus', 0.0)
                pivot_status = "❌ 構造確証なし"
                if pivot_bonus > 0:
                     pivot_status = f"✅ **構造的S/R確証**"
                analysis_detail += f"   └ **構造分析(Pivot)**: {pivot_status} (<ins>**+{pivot_bonus * 100:.2f}点 ボーナス**</ins>)\n"

                # NEW: 出来高確証ボーナスのハイライト
                volume_bonus = tech_data.get('volume_confirmation_bonus', 0.0)
                if volume_bonus > 0:
                    analysis_detail += f"   └ **出来高/流動性確証**: ✅ <ins>**+{volume_bonus * 100:.2f}点 ボーナス追加**</ins> (平均比率: {tech_data.get('volume_ratio', 0.0):.1f}x)\n"
                else:
                    analysis_detail += f"   └ **出来高/流動性確証**: ❌ 確認なし (比率: {tech_data.get('volume_ratio', 0.0):.1f}x)\n"
                
                # NEW: Funding Rateボーナス/ペナルティのハイライト
                funding_rate_val = tech_data.get('funding_rate_value', 0.0)
                funding_rate_bonus = tech_data.get('funding_rate_bonus_value', 0.0)
                funding_rate_status = ""
                if funding_rate_bonus > 0:
                    funding_rate_status = f"✅ **優位性あり** (<ins>**+{funding_rate_bonus * 100:.2f}点**</ins>)"
                elif funding_rate_bonus < 0:
                    funding_rate_status = f"⚠️ **過密ペナルティ適用** (<ins>**-{abs(funding_rate_bonus) * 100:.2f}点**</ins>)"
                else:
                    funding_rate_status = "❌ フィルター範囲外"
                
                analysis_detail += f"   └ **資金調達率 (FR)**: {funding_rate_val * 100:.4f}% (8h) - {funding_rate_status}\n"

                # NEW: Dominance Biasボーナス/ペナルティのハイライト
                dominance_trend = tech_data.get('dominance_trend', 'Neutral')
                dominance_bonus = tech_data.get('dominance_bias_bonus_value', 0.0)
                
                dominance_status = ""
                if dominance_bonus > 0:
                    dominance_status = f"✅ **優位性あり** (<ins>**+{dominance_bonus * 100:.2f}点**</ins>)"
                elif dominance_bonus < 0:
                    dominance_status = f"⚠️ **バイアスにより減点適用** (<ins>**-{abs(dominance_bonus) * 100:.2f}点**</ins>)"
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
        f"| ⚙️ **BOT Ver** | **v17.0.0** - Structural Analysis & PnL Estimate |\n" # バージョン更新
        f"==================================\n"
        f"\n<pre>※ Limit注文は、価格が指定水準に到達した際のみ約定します。DTS戦略では、価格が有利な方向に動いた場合、SLが自動的に追跡され利益を最大化します。</pre>"
    )

    return header + trade_plan + sr_info + analysis_detail + footer


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

            if len(df_eth) >= 2:
                eth_change = (df_eth['close'].iloc[-1] - df_eth['close'].iloc[-2]) / df_eth['close'].iloc[-2]


    # 2. FGI Proxyの計算 (恐怖指数/市場センチメント)
    sentiment_score = 0.0
    if btc_trend == 1 and eth_trend == 1:
        sentiment_score = 0.07 # 強いリスクオン (ボーナス強化)
    elif btc_trend == -1 and eth_trend == -1:
        sentiment_score = -0.07 # 強いリスクオフ (恐怖) (ボーナス強化)
    
    # 3. BTC Dominance Proxyの計算
    dominance_trend = "Neutral"
    dominance_bias_score = 0.0
    
    DOM_DIFF_THRESHOLD = 0.002 # 0.2%以上の差でドミナンスに偏りありと判断
    
    if status_btc == "Success" and status_eth == "Success" and len(df_btc) >= 2 and len(df_eth) >= 2:
        
        # BTCとETHの直近の価格変化率の差を計算 (BTCドミナンスの変化の代理変数)
        dom_diff = btc_change - eth_change 
        
        if dom_diff > DOM_DIFF_THRESHOLD:
            # BTCの上昇率がETHより高い -> BTC dominanceの上昇トレンド
            dominance_trend = "Increasing"
            dominance_bias_score = DOMINANCE_BIAS_BONUS_PENALTY # Altcoinにペナルティ
            
        elif dom_diff < -DOM_DIFF_THRESHOLD:
            # ETHの上昇率がBTCより高い -> BTC dominanceの下降トレンド
            dominance_trend = "Decreasing"
            dominance_bias_score = -DOMINANCE_BIAS_BONUS_PENALTY # Altcoinにボーナス
            
        else:
            dominance_trend = "Neutral"
            dominance_bias_score = 0.0


    macro_context = {
        "btc_trend": btc_trend,
        "eth_trend": eth_trend,
        "sentiment_fgi_proxy": sentiment_score,
        "dominance_trend": dominance_trend,
        "dominance_bias_score": dominance_bias_score
    }
    
    logging.info(f"✅ マクロコンテキスト更新: BTC Trend: {btc_trend}, FGI Proxy: {sentiment_score:.2f}, Dominance Bias: {dominance_bias_score:.2f} ({dominance_trend})")
    
    return macro_context


# ====================================================================================
# TRADING STRATEGY & SCORING LOGIC
# ====================================================================================

def calculate_pivot_points(df: pd.DataFrame) -> Dict[str, float]:
    """
    Pivot Point (PP) と主要なサポート/レジスタンス (S/R) を計算する
    Daily Pivot (Classic) を使用
    """
    
    # 前日のローソク足の情報を取得
    if len(df) < 2:
        return {}
        
    prev_day = df.iloc[-2] 
    
    high = prev_day['high']
    low = prev_day['low']
    close = prev_day['close']
    
    # Pivot Point (PP)
    pp = (high + low + close) / 3
    
    # Resistance Levels
    r1 = (2 * pp) - low
    r2 = pp + (high - low)
    r3 = r2 + (high - low)
    
    # Support Levels
    s1 = (2 * pp) - high
    s2 = pp - (high - low)
    s3 = s2 - (high - low)
    
    return {
        'pp': pp, 
        'r1': r1, 'r2': r2, 'r3': r3,
        's1': s1, 's2': s2, 's3': s3
    }


def analyze_single_timeframe(symbol: str, timeframe: str, ohlcv: List[List[float]], macro_context: Dict) -> Dict:
    """単一の時間足に基づいた詳細なテクニカル分析とシグナルスコアリングを実行する"""
    
    if not ohlcv:
        return {'symbol': symbol, 'timeframe': timeframe, 'side': 'DataShortage', 'score': 0.0}

    # 1. データフレームの準備
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['close'] = pd.to_numeric(df['close'], errors='coerce').astype('float64')
    df['high'] = pd.to_numeric(df['high'], errors='coerce').astype('float64')
    df['low'] = pd.to_numeric(df['low'], errors='coerce').astype('float64')
    df['open'] = pd.to_numeric(df['open'], errors='coerce').astype('float64')
    df['volume'] = pd.to_numeric(df['volume'], errors='coerce').astype('float64')
    
    # NaNチェック
    if df['close'].isnull().any() or df.empty:
        return {'symbol': symbol, 'timeframe': timeframe, 'side': 'DataShortage', 'score': 0.0}
    
    # 最終価格
    current_price = df['close'].iloc[-1]
    
    # 2. テクニカル指標の計算
    df.ta.rsi(length=14, append=True)
    df.ta.macd(fast=12, slow=26, signal=9, append=True)
    df.ta.adx(length=14, append=True)
    df.ta.atr(length=14, append=True)
    df.ta.stoch(k=14, d=3, smooth_k=3, append=True)
    df.ta.cci(length=20, append=True)
    df.ta.bbands(length=20, append=True)
    
    # VWAP (20期間)
    df['vwap'] = (df['close'] * df['volume']).cumsum() / df['volume'].cumsum()
    df['vwap_mid'] = df['vwap'].iloc[-1]
    
    # SMA (長期トレンド判定用)
    df['sma_long'] = ta.sma(df['close'], length=LONG_TERM_SMA_LENGTH)
    
    # Pivot Points (日足で計算されたS/Rを全ての時間軸で使用)
    pivot_points = calculate_pivot_points(df)

    # NaN除去と最終行の取得
    df.dropna(inplace=True)
    if df.empty:
        return {'symbol': symbol, 'timeframe': timeframe, 'side': 'DataShortage', 'score': 0.0}
        
    last = df.iloc[-1]
    
    # 3. スコアリングの初期設定
    score: float = BASE_SCORE # 初期スコア 0.40
    side: str = 'Neutral'
    reversal_signal: bool = False
    
    # ----------------------------------------------
    # 4. ベースシグナル判定 (RSI, ADX, MACD, CCI)
    # ----------------------------------------------
    
    # RSI
    rsi_val = last['RSI_14']
    
    # Long/Buy Signal
    if rsi_val <= RSI_OVERSOLD and last['CCI_20'] < -100:
        score += 0.20
        reversal_signal = True
        side = 'ロング'
    elif rsi_val <= RSI_MOMENTUM_LOW:
        score += 0.10
        side = 'ロング'

    # Short/Sell Signal
    if rsi_val >= RSI_OVERBOUGHT and last['CCI_20'] > 100:
        score += 0.20
        reversal_signal = True
        side = 'ショート'
    elif rsi_val >= RSI_MOMENTUM_HIGH:
        score += 0.10
        side = 'ショート'
        
    # ADX (トレンドの強さ)
    adx_val = last['ADX_14']
    regime = 'レンジ/もみ合い (Range)'
    if adx_val >= ADX_TREND_THRESHOLD:
        score += 0.15 
        regime = 'トレンド (Trend)'
    elif adx_val >= 20:
        score += 0.05
        regime = '初期トレンド (Emerging Trend)'
    
    # MACD Cross Confirmation (モメンタム)
    macd_hist = last['MACDH_12_26_9']
    macd_cross_valid = True
    macd_cross_penalty_value = 0.0
    
    if side == 'ロング':
        # ロングシグナルだがMACDヒストグラムが下降中（マイナスだが減少傾向ではない）
        if macd_hist < 0 and (last['MACDH_12_26_9'] < df['MACDH_12_26_9'].iloc[-2]):
            score -= MACD_CROSS_PENALTY 
            macd_cross_valid = False
            macd_cross_penalty_value = MACD_CROSS_PENALTY
            
    elif side == 'ショート':
        # ショートシグナルだがMACDヒストグラムが上昇中（プラスだが減少傾向ではない）
        if macd_hist > 0 and (last['MACDH_12_26_9'] > df['MACDH_12_26_9'].iloc[-2]):
            score -= MACD_CROSS_PENALTY
            macd_cross_valid = False
            macd_cross_penalty_value = MACD_CROSS_PENALTY

    # ----------------------------------------------
    # 5. 価格アクション・長期トレンド・ボラティリティフィルター
    # ----------------------------------------------
    
    # Long Term Trend Filter (4h足でのみ適用、それ以外はスコアペナルティとして使用)
    long_term_trend = 'Neutral'
    long_term_reversal_penalty = False
    long_term_reversal_penalty_value = 0.0

    if not last.get('sma_long', np.nan) is np.nan:
        if current_price > last['sma_long']:
            long_term_trend = 'Long'
        elif current_price < last['sma_long']:
            long_term_trend = 'Short'
            
    # Long-Term Reversal Penalty (15m/1hで長期トレンドに逆行するシグナルの場合)
    if timeframe != '4h' and side != 'Neutral':
        if long_term_trend == 'Long' and side == 'ショート':
            score -= LONG_TERM_REVERSAL_PENALTY
            long_term_reversal_penalty = True
            long_term_reversal_penalty_value = LONG_TERM_REVERSAL_PENALTY
        elif long_term_trend == 'Short' and side == 'ロング':
            score -= LONG_TERM_REVERSAL_PENALTY
            long_term_reversal_penalty = True
            long_term_reversal_penalty_value = LONG_TERM_REVERSAL_PENALTY
            
    # Volatility Filter (ボリンジャーバンド幅のチェック) - 15m/1hでのみ適用
    bb_width = last['BBP_20_2.0'] * 100 # BBPは0-100なので*100
    volatility_penalty = 0.0
    if timeframe in ['15m', '1h']:
        if bb_width >= VOLATILITY_BB_PENALTY_THRESHOLD: # BB幅が広すぎる場合（急騰/急落後）
            score -= 0.10
            volatility_penalty = 0.10
    
    # 出来高確認 (Volume Confirmation) - 平均出来高と比較
    volume_confirmation_bonus = 0.0
    volume_ratio = 0.0
    
    if len(df) > 100:
        # 直近の出来高と過去100期間の平均出来高を比較
        avg_volume = df['volume'].iloc[-100:-1].mean()
        current_volume = last['volume']
        
        if avg_volume > 0:
            volume_ratio = current_volume / avg_volume
            if volume_ratio >= VOLUME_CONFIRMATION_MULTIPLIER:
                volume_confirmation_bonus = 0.05
                score += volume_confirmation_bonus
    
    # VWAP Consistency (VWAPから離れすぎていないか)
    vwap_consistent = True
    if side == 'ロング' and current_price < last['vwap_mid']:
         # ロングシグナルなのに価格がVWAPより下にある -> スコア減点なし、VWAP不一致フラグのみ
         vwap_consistent = False
    elif side == 'ショート' and current_price > last['vwap_mid']:
         # ショートシグナルなのに価格がVWAPより上にある -> スコア減点なし、VWAP不一致フラグのみ
         vwap_consistent = False

    # StochRSI Filter (過熱感のチェック) - 15m/1hでのみ適用
    stoch_penalty = 0.0
    stoch_k = last['STOCHk_14_3_3']
    stoch_d = last['STOCHd_14_3_3']
    
    if timeframe in ['15m', '1h'] and side != 'Neutral':
        if side == 'ロング':
            # ロングシグナルだがStochRSIが買われすぎ水準 (80以上)
            if stoch_k > 80 or stoch_d > 80:
                score -= 0.05
                stoch_penalty = 0.05
        elif side == 'ショート':
            # ショートシグナルだがStochRSIが売られすぎ水準 (20以下)
            if stoch_k < 20 or stoch_d < 20:
                score -= 0.05
                stoch_penalty = 0.05


    # ----------------------------------------------
    # 6. SL/TP/RRRの決定 (DTS戦略ベース)
    # ----------------------------------------------
    
    atr_val = last['ATR_14']
    rr_ratio = DTS_RRR_DISPLAY # 表示用
    
    entry_price = current_price
    sl_price = 0.0
    tp1_price = 0.0
    
    entry_type = 'Market' 
    structural_sl_used = False
    structural_pivot_bonus = 0.0

    # 構造的SL (S1/R1) の探索と適用
    if pivot_points and side != 'Neutral':
        if side == 'ロング':
            # Long: S1/S2/PPがエントリー価格より下で、ATRの 2.0倍 以内にあるか
            close_s1 = pivot_points.get('s1', 0.0)
            close_s2 = pivot_points.get('s2', 0.0)
            close_pp = pivot_points.get('pp', 0.0)
            
            potential_sls = sorted([sl for sl in [close_s1, close_s2, close_pp] 
                                    if sl < current_price and current_price - sl <= atr_val * ATR_TRAIL_MULTIPLIER], 
                                    reverse=True) # 近いものから優先
            
            if potential_sls:
                # S/Rに一致するSLを採用し、0.5 ATRのバッファを追加 (FIX v16.0.1)
                structural_sl_used = True
                sl_price_raw = potential_sls[0]
                sl_price = sl_price_raw - (0.5 * atr_val) 
                
                # S/Rに一致したためボーナスを追加
                structural_pivot_bonus = 0.05
                score += structural_pivot_bonus

                # 構造的なエントリーポイントを逆算 (S/RでエントリーするLimit注文)
                entry_point_raw = sl_price_raw # S1/S2/PPの位置をLimitエントリーポイントとする
                entry_price = entry_point_raw
                entry_type = 'Limit'
                
                # Limit注文が約定しないリスクを考慮し、ここではエントリーポイントをLimit価格として表示

                # 構造的SL (S1/R1) が有効な場合、RRRは高くなると想定
                rr_ratio = max(rr_ratio, 3.0) 
            else:
                # 構造的SLが見つからなければATRベースSL
                sl_price = current_price - (atr_val * ATR_TRAIL_MULTIPLIER)
                entry_type = 'Market'
                entry_price = current_price # SL計算のため、いったん現在価格で計算
        
        elif side == 'ショート':
            # Short: R1/R2/PPがエントリー価格より上で、ATRの 2.0倍 以内にあるか
            close_r1 = pivot_points.get('r1', 0.0)
            close_r2 = pivot_points.get('r2', 0.0)
            close_pp = pivot_points.get('pp', 0.0)
            
            potential_sls = sorted([sl for sl in [close_r1, close_r2, close_pp] 
                                    if sl > current_price and sl - current_price <= atr_val * ATR_TRAIL_MULTIPLIER]) # 近いものから優先
            
            if potential_sls:
                # S/Rに一致するSLを採用し、0.5 ATRのバッファを追加 (FIX v16.0.1)
                structural_sl_used = True
                sl_price_raw = potential_sls[0]
                sl_price = sl_price_raw + (0.5 * atr_val)
                
                # S/Rに一致したためボーナスを追加
                structural_pivot_bonus = 0.05
                score += structural_pivot_bonus
                
                # 構造的なエントリーポイントを逆算 (R1/R2/PPでエントリーするLimit注文)
                entry_point_raw = sl_price_raw
                entry_price = entry_point_raw
                entry_type = 'Limit'
                
                # 構造的SL (S1/R1) が有効な場合、RRRは高くなると想定
                rr_ratio = max(rr_ratio, 3.0) 
            else:
                # 構造的SLが見つからなければATRベースSL
                sl_price = current_price + (atr_val * ATR_TRAIL_MULTIPLIER)
                entry_type = 'Market'
                entry_price = current_price # SL計算のため、いったん現在価格で計算

    else:
        # Pivot Pointsが計算できない場合、純粋なATR SLを使用
        if side == 'ロング':
            sl_price = current_price - (atr_val * ATR_TRAIL_MULTIPLIER)
        elif side == 'ショート':
            sl_price = current_price + (atr_val * ATR_TRAIL_MULTIPLIER)
        entry_type = 'Market'
        entry_price = current_price


    # TP1 (目標値: ATRベースSL幅のDTS_RRR_DISPLAY倍。これは動的決済の目安としてのみ使用)
    risk_width = abs(entry_price - sl_price)
    if side == 'ロング':
        tp1_price = entry_price + (risk_width * DTS_RRR_DISPLAY) 
    elif side == 'ショート':
        tp1_price = entry_price - (risk_width * DTS_RRR_DISPLAY) 
        
    # SL/TPが有効でない場合はNeutralにする
    if sl_price <= 0 or (side == 'ロング' and sl_price >= entry_price) or (side == 'ショート' and sl_price <= entry_price):
         side = 'Neutral'
         score = BASE_SCORE # リセット
         
    # リスクリワード計算
    # 構造的SLの場合、SL幅は狭くなり、実質RRRは高くなるが、表示は固定RRRを上限とする
    if side != 'Neutral' and risk_width > 0:
        # DTSのため、RRRは固定値ではなく目標値として表示
        pass 
    else:
        rr_ratio = 0.0


    # ----------------------------------------------
    # 7. マクロフィルター (Funding Rate Bias & Dominance Bias)
    # ----------------------------------------------
    
    funding_rate_value = 0.0
    funding_rate_bonus_value = 0.0
    
    # Funding Rate Filter (グローバルコンテキストから取得)
    if side != 'Neutral' and macro_context:
        funding_rate_value = macro_context.get(f'{symbol}_fr', 0.0)
        
        if side == 'ロング':
            # Long: FRがマイナス方向で過熱している (ショートが多い) -> ボーナス
            if funding_rate_value <= -FUNDING_RATE_THRESHOLD:
                score += FUNDING_RATE_BONUS_PENALTY
                funding_rate_bonus_value = FUNDING_RATE_BONUS_PENALTY
            # Long: FRがプラス方向で過熱している (ロングが多い) -> ペナルティ
            elif funding_rate_value >= FUNDING_RATE_THRESHOLD:
                score -= FUNDING_RATE_BONUS_PENALTY
                funding_rate_bonus_value = -FUNDING_RATE_BONUS_PENALTY
        
        elif side == 'ショート':
            # Short: FRがプラス方向で過熱している (ロングが多い) -> ボーナス
            if funding_rate_value >= FUNDING_RATE_THRESHOLD:
                score += FUNDING_RATE_BONUS_PENALTY
                funding_rate_bonus_value = FUNDING_RATE_BONUS_PENALTY
            # Short: FRがマイナス方向で過熱している (ショートが多い) -> ペナルティ
            elif funding_rate_value <= -FUNDING_RATE_THRESHOLD:
                score -= FUNDING_RATE_BONUS_PENALTY
                funding_rate_bonus_value = -FUNDING_RATE_BONUS_PENALTY


    # Dominance Bias Filter (Altcoinにのみ適用)
    dominance_bias_bonus_value = 0.0
    dominance_trend = macro_context.get('dominance_trend', 'Neutral')
    
    if symbol != 'BTC-USDT' and side != 'Neutral':
        bias_score_raw = macro_context.get('dominance_bias_score', 0.0)
        
        if bias_score_raw > 0: # BTCドミナンス上昇トレンド (Altcoinに不利)
            if side == 'ロング':
                score -= bias_score_raw
                dominance_bias_bonus_value = -bias_score_raw
            elif side == 'ショート':
                score += bias_score_raw / 2 # ショートは優位性緩和
                dominance_bias_bonus_value = bias_score_raw / 2
                
        elif bias_score_raw < 0: # BTCドミナンス下降トレンド (Altcoinに有利)
            if side == 'ロング':
                score += abs(bias_score_raw)
                dominance_bias_bonus_value = abs(bias_score_raw)
            elif side == 'ショート':
                score -= abs(bias_score_raw) / 2 # ショートは不利緩和
                dominance_bias_bonus_value = -abs(bias_score_raw) / 2


    # 8. 最終スコア調整とクリッピング
    score = max(0.0, min(1.0, score)) # 0.0から1.0にクリップ
    
    if score < BASE_SCORE:
         side = 'Neutral'
         score = BASE_SCORE # ベーススコア未満なら無効なシグナルとしてリセット
         
    # 最終的なLong/Short判定
    if score >= SIGNAL_THRESHOLD and side == 'ロング':
        pass
    elif score >= SIGNAL_THRESHOLD and side == 'ショート':
        pass
    else:
        side = 'Neutral'
        
    
    # 9. 結果の格納
    result = {
        'symbol': symbol,
        'timeframe': timeframe,
        'side': side,
        'score': score,
        'price': current_price,
        'entry': entry_price,
        'sl': sl_price,
        'tp1': tp1_price,
        'rr_ratio': rr_ratio,
        'regime': regime,
        'entry_type': entry_type,
        'macro_context': macro_context,
        'tech_data': {
            'rsi': rsi_val,
            'adx': adx_val,
            'macd_hist': macd_hist,
            'cci': last['CCI_20'],
            'atr_value': atr_val,
            'stoch_k': stoch_k,
            'stoch_d': stoch_d,
            'stoch_filter_penalty': stoch_penalty,
            'long_term_trend': long_term_trend,
            'long_term_reversal_penalty': long_term_reversal_penalty,
            'long_term_reversal_penalty_value': long_term_reversal_penalty_value,
            'macd_cross_valid': macd_cross_valid,
            'macd_cross_penalty_value': macd_cross_penalty_value,
            'volatility_penalty': volatility_penalty,
            'volume_ratio': volume_ratio,
            'volume_confirmation_bonus': volume_confirmation_bonus,
            'vwap_consistent': vwap_consistent,
            'funding_rate_value': funding_rate_value,
            'funding_rate_bonus_value': funding_rate_bonus_value,
            'dominance_trend': dominance_trend,
            'dominance_bias_bonus_value': dominance_bias_bonus_value,
            'structural_sl_used': structural_sl_used,
            'structural_pivot_bonus': structural_pivot_bonus,
            'pivot_points': pivot_points # NEW: Pivot Pointsを含める
        }
    }
    
    return result


async def run_integrated_analysis(symbol: str) -> List[Dict]:
    """3つの時間足で分析を実行し、結果を統合する"""
    
    timeframes = ['15m', '1h', '4h']
    tasks = []
    
    # OHLCVデータを並行して取得
    for tf in timeframes:
        tasks.append(fetch_ohlcv_with_fallback(CCXT_CLIENT_NAME, symbol, tf))

    ohlcv_results = await asyncio.gather(*tasks)
    
    # マクロコンテキストの取得
    global GLOBAL_MACRO_CONTEXT
    macro_context = GLOBAL_MACRO_CONTEXT

    # テクニカル分析を実行
    analysis_results = []
    for (ohlcv, status, client), tf in zip(ohlcv_results, timeframes):
        if status == "Success":
            result = analyze_single_timeframe(symbol, tf, ohlcv, macro_context)
        else:
            result = {'symbol': symbol, 'timeframe': tf, 'side': status, 'score': 0.0}
            
        analysis_results.append(result)
        
    return analysis_results


# ====================================================================================
# MAIN LOOP
# ====================================================================================

async def main_loop():
    """メインのボット実行ループ"""
    global LAST_UPDATE_TIME, LAST_SUCCESS_TIME, LAST_ANALYSIS_SIGNALS, GLOBAL_MACRO_CONTEXT

    # 初期化
    await initialize_ccxt_client()

    while True:
        try:
            current_time = time.time()
            LAST_UPDATE_TIME = current_time

            # 1. 銘柄リストの更新 (60分ごと)
            if current_time - LAST_SUCCESS_TIME > 60 * 60: 
                await update_symbols_by_volume()
                logging.info(f"✅ 銘柄リストを更新しました。監視数: {len(CURRENT_MONITOR_SYMBOLS)}")
            
            # 2. マクロコンテキストとFunding Rateの更新 (180秒ごと)
            GLOBAL_MACRO_CONTEXT = await get_crypto_macro_context()
            
            # 各銘柄のFunding Rateを取得し、マクロコンテキストに追加
            funding_rate_tasks = []
            for symbol in CURRENT_MONITOR_SYMBOLS:
                 funding_rate_tasks.append(fetch_funding_rate(symbol))
            
            funding_rates = await asyncio.gather(*funding_rate_tasks)
            
            for symbol, fr in zip(CURRENT_MONITOR_SYMBOLS, funding_rates):
                 GLOBAL_MACRO_CONTEXT[f'{symbol}_fr'] = fr
                 
            logging.info(f"✅ Funding Rateデータを更新しました。")

            # 3. 全銘柄の統合分析を実行
            analysis_tasks = []
            for symbol in CURRENT_MONITOR_SYMBOLS:
                analysis_tasks.append(run_integrated_analysis(symbol))
            
            all_analysis_results = await asyncio.gather(*analysis_tasks)
            
            # 4. シグナルのフィルタリングとスコアリング
            current_signals: List[Dict] = []
            
            for symbol_results in all_analysis_results:
                
                # 有効なシグナルのみを抽出 (Neutral, Error, DataShortageを除く)
                valid_signals = [s for s in symbol_results if s.get('side') not in ["DataShortage", "ExchangeError", "Neutral"]]
                
                if not valid_signals:
                    continue
                    
                # スコアが閾値を超えているもの
                high_score_signals = [s for s in valid_signals if s.get('score', 0.5) >= SIGNAL_THRESHOLD]
                
                if not high_score_signals:
                    continue
                    
                # 最高のシグナルを特定
                best_signal = max(
                    high_score_signals, 
                    key=lambda s: (
                        s.get('score', 0.5), 
                        s.get('rr_ratio', 0.0), 
                        s.get('tech_data', {}).get('adx', 0.0)
                    )
                )
                
                # 最終的な取引シグナルとして格納
                current_signals.append(best_signal)


            # 5. シグナルのランク付けと通知
            
            # スコアとRRRで降順ソート
            sorted_signals = sorted(
                current_signals, 
                key=lambda s: (
                    s.get('score', 0.5), 
                    s.get('rr_ratio', 0.0), 
                    s.get('tech_data', {}).get('adx', 0.0)
                ),
                reverse=True
            )
            
            LAST_ANALYSIS_SIGNALS = sorted_signals[:TOP_SIGNAL_COUNT] # APIステータス用に格納
            
            logging.info(f"✅ 全銘柄の分析が完了しました。有効な高スコアシグナル: {len(current_signals)} 件")


            for rank, signal in enumerate(sorted_signals[:TOP_SIGNAL_COUNT], 1):
                symbol = signal['symbol']
                
                # クールダウンチェック
                if current_time - TRADE_NOTIFIED_SYMBOLS.get(symbol, 0) < TRADE_SIGNAL_COOLDOWN:
                    logging.info(f"➡️ {symbol} はクールダウン中のためスキップします。")
                    continue
                
                # Telegramメッセージを生成
                message = format_integrated_analysis_message(symbol, all_analysis_results[CURRENT_MONITOR_SYMBOLS.index(symbol)], rank)
                
                if message:
                    if send_telegram_html(message):
                        TRADE_NOTIFIED_SYMBOLS[symbol] = current_time
                        logging.info(f"📢 {symbol} のシグナル (ランク: {rank}) を通知しました。")
                    else:
                        logging.error(f"❌ {symbol} のTelegram通知に失敗しました。")

            
            # 6. 次のループまで待機
            LAST_SUCCESS_TIME = time.time()
            await asyncio.sleep(LOOP_INTERVAL)

        except Exception as e:
            error_name = type(e).__name__
            logging.error(f"メインループで致命的なエラー: {error_name}")
            await asyncio.sleep(60)


# ====================================================================================
# FASTAPI SETUP
# ====================================================================================

app = FastAPI(title="Apex BOT API", version="v17.0.0 - Structural Analysis & PnL Estimate") # バージョン更新

@app.on_event("startup")
async def startup_event():
    logging.info("🚀 Apex BOT v17.0.0 Startup initializing...") # バージョン更新
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
        "bot_version": "v17.0.0 - Structural Analysis & PnL Estimate", # バージョン更新
        "last_success_time_utc": datetime.fromtimestamp(LAST_SUCCESS_TIME, tz=timezone.utc).isoformat() if LAST_SUCCESS_TIME else "N/A",
        "current_client": CCXT_CLIENT_NAME,
        "monitoring_symbols": len(CURRENT_MONITOR_SYMBOLS),
        "last_signals_count": len(LAST_ANALYSIS_SIGNALS)
    }
    return JSONResponse(content=status_msg)

@app.head("/")
@app.get("/")
def home_view():
    return JSONResponse(content={"message": "Apex BOT is running (v17.0.0)", "status_endpoint": "/status"})
