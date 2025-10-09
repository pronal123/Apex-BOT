# ====================================================================================
# Apex BOT v17.0.21 - FIX: Persistent Indicator Calculation Failure (VWAP Fallback)
# - FIX: pandas_taが指標カラム、特にVWAPを生成できない場合に、手動でVWAPを計算するフォールバックロジックを追加し、
#        「ATR_14, BBL_20_2.0, BBU_20_2.0, VWAPがありません」というエラーログの大量発生をさらに抑制します。
# - ADD: データクリーンアップ後のDataFrameの長さをログに出力するよう変更しました (DEBUGレベル)。
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

# --- 変更: ユーザー要望によりクールダウン無効化と閾値無視 ---
TRADE_SIGNAL_COOLDOWN = 1           # ほぼ無効化 (1秒)
SIGNAL_THRESHOLD = 0.00             # 閾値を無効化し、常に最高スコアを採用
TOP_SIGNAL_COUNT = 3                
REQUIRED_OHLCV_LIMITS = {'15m': 500, '1h': 500, '4h': 500} 
MINIMUM_DATAFRAME_LENGTH = 50 # v17.0.20: クリーンアップ後の最小データ長
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

# --- 復元: PNL計算用の定数 ---
POSITION_CAPITAL = 10000            # 損益予測に使用するポジションサイズ (USD)

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

# --- 復元: PNL計算とフォーマットのユーティリティ ---
def format_pnl_utility_telegram(pnl: float) -> str:
    """損益額を符号付きでHTML形式に整形 (緑：利益、赤：損失)"""
    if pnl is None: return "N/A"
    if pnl >= 0:
        return f"<b><span style='color:#00ff00'>+${pnl:,.2f}</span></b>"
    else:
        return f"<b><span style='color:#ff0000'>-${abs(pnl):,.2f}</span></b>"

def calculate_pnl_at_pivot(target_price: float, entry_price: float, is_long: bool, capital: float) -> float:
    """
    指定された価格レベルに到達した場合の概算損益 (1xレバレッジ) を計算
    """
    if target_price <= 0 or entry_price <= 0: return 0.0
    
    price_change_rate = (target_price - entry_price) / entry_price
    
    if is_long:
        pnl = capital * price_change_rate
    else: # Short
        pnl = capital * -price_change_rate
        
    return pnl
# --- 復元ここまで ---


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
    3つの時間軸の分析結果を統合し、ログメッセージの形式に整形する (v17.0.20対応 - PNLブロック復元)
    """
    
    valid_signals = [s for s in signals if s.get('side') not in ["DataShortage", "ExchangeError", "Neutral"]]
    
    if not valid_signals:
        return "" 
        
    # 最もスコアが高いシグナルを採用
    # FIX v17.0.19/20: キーの存在チェックを強化
    best_signal = max(
        valid_signals, 
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
    is_long = (side == "ロング")
    
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
    
    exit_type_str = "DTS (動的追跡損切)" 
    time_to_tp = get_tp_reach_time(timeframe)

    header = (
        f"--- 🟢 --- **{display_symbol}** --- 🟢 ---\n"
        f"{rank_header} 📈 {strength} 発生！ - {direction_emoji}{market_sentiment_str}\n" 
        f"==================================\n"
        f"| 🎯 **予測勝率** | **<ins>{win_rate:.1f}%</ins>** | **条件{strength.split(' ')[0]}** |\n"
        f"| 💯 **分析スコア** | <b>{score_100:.2f} / 100.00 点</b> (ベース: {timeframe}足) |\n" 
        f"| ⏰ **決済戦略** | **{exit_type_str}** (目標RRR: 1:{rr_ratio:.2f}+) |\n" 
        f"| ⏳ **TP到達目安** | **{time_to_tp}** | (変動する可能性があります) |\n"
        f"==================================\n"
    )

    sl_source_str = "ATR基準"
    if best_signal.get('tech_data', {}).get('structural_sl_used', False):
        sl_source_str = "構造的 (Pivot) + **0.5 ATR バッファ**" 
        
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
    # 2. PNL (損益) ブロックの復元
    # ----------------------------------------------------
    
    # SL/TPの概算損益計算 (1x想定)
    sl_loss_usd = calculate_pnl_at_pivot(sl_price, entry_price, is_long, POSITION_CAPITAL)
    tp_gain_usd = calculate_pnl_at_pivot(tp_price, entry_price, is_long, POSITION_CAPITAL)
    
    # 損益率の計算 (絶対値)
    sl_risk_percent = abs(sl_loss_usd / POSITION_CAPITAL * 100)
    tp_gain_percent = abs(tp_gain_usd / POSITION_CAPITAL * 100)

    pnl_prediction_block = (
        f"\n**💵 損益予測 ({POSITION_CAPITAL:,.0f} USD ポジションの場合) [1x想定]**\n"
        f"----------------------------------\n"
        f"| 項目 | **損益額 (USD)** | 損益率 (対ポジションサイズ) |\n"
        f"| :--- | :--- | :--- |\n"
        f"| ❌ SL実行時 | **{format_pnl_utility_telegram(sl_loss_usd)}** | {sl_risk_percent:.2f}% |\n" 
        f"| 🟢 TP目標時 | **{format_pnl_utility_telegram(tp_gain_usd)}** | {tp_gain_percent:.2f}% |\n"
        f"| ----------------------------------\n"
    )
    
    # Pivot PNLの計算とブロック生成
    pivot_points = best_signal.get('tech_data', {}).get('pivot_points', {})
    
    pivot_pnl_block = ""
    pivot_r1 = pivot_points.get('R1', 0.0)
    pivot_r2 = pivot_points.get('R2', 0.0) 
    pivot_s1 = pivot_points.get('S1', 0.0)
    pivot_s2 = pivot_points.get('S2', 0.0)
    
    # R2/S2が計算されていない場合の代替値 (R1/S1からATRを引いた/足した値を利用)
    atr_value = best_signal.get('tech_data', {}).get('atr_value', 0.0)

    if pivot_r1 > 0 and entry_price > 0 and side in ["ロング", "ショート"]:
        
        # calculate_fib_pivotで計算されたR1/S1を使用
        pnl_r1 = calculate_pnl_at_pivot(pivot_r1, entry_price, is_long, POSITION_CAPITAL)
        pnl_s1 = calculate_pnl_at_pivot(pivot_s1, entry_price, is_long, POSITION_CAPITAL)
        
        # R2, S2の価格が技術的に計算されていないため、ここではR1/S1の価格とATRを単純に使用 (v17.0.14の構造維持)
        if pivot_r2 == 0.0:
            pivot_r2 = pivot_r1 + atr_value * 1.5 
        if pivot_s2 == 0.0:
            pivot_s2 = pivot_s1 - atr_value * 1.5
            
        pnl_r2 = calculate_pnl_at_pivot(pivot_r2, entry_price, is_long, POSITION_CAPITAL)
        pnl_s2 = calculate_pnl_at_pivot(pivot_s2, entry_price, is_long, POSITION_CAPITAL)

        
        pivot_pnl_block = (
            f"\n**🧮 ${POSITION_CAPITAL:,.0f} ポジションの到達損益 (Pivot S/R) [1x想定]**\n"
            f"----------------------------------\n"
            f"| 目標レベル | **価格 (USD)** | 損益 (概算) |\n"
            f"| :--- | :--- | :--- |\n"
            f"| 📈 **抵抗線 R1** | <code>${format_price_utility(pivot_r1, symbol)}</code> | {format_pnl_utility_telegram(pnl_r1)} |\n"
            f"| 🚨 **抵抗線 R2** | <code>${format_price_utility(pivot_r2, symbol)}</code> | {format_pnl_utility_telegram(pnl_r2)} |\n"
            f"| 📉 **支持線 S1** | <code>${format_price_utility(pivot_s1, symbol)}</code> | {format_pnl_utility_telegram(pnl_s1)} |\n"
            f"| 🧊 **支持線 S2** | <code>${format_price_utility(pivot_s2, symbol)}</code> | {format_pnl_utility_telegram(pnl_s2)} |\n"
            f"----------------------------------\n"
        )


    # ----------------------------------------------------
    # 3. 統合分析サマリーとスコアリングの詳細
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
            
            penalty_status = f" (逆張りペナルティ: -{tech_data.get('long_term_reversal_penalty_value', 0.0) * 100:.1f}点適用)" if tech_data.get('long_term_reversal_penalty') else ""
            
            momentum_valid = tech_data.get('macd_cross_valid', True)
            momentum_text = "[✅ モメンタム確証: OK]" if momentum_valid else f"[⚠️ モメンタム反転により減点: -{tech_data.get('macd_cross_penalty_value', 0.0) * 100:.1f}点]"

            vwap_consistent = tech_data.get('vwap_consistent', False)
            vwap_text = "[🌊 VWAP一致: OK]" if vwap_consistent else "[🌊 VWAP不一致: NG]"

            stoch_penalty = tech_data.get('stoch_filter_penalty', 0.0)
            stoch_text = ""
            if stoch_penalty > 0:
                 stoch_text = f" [⚠️ STOCHRSI 過熱感により減点: -{stoch_penalty * 100:.2f}点]"
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
                # RSI/MACDH/CCI
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
                dominance_trend = tech_data.get('dominance_trend', 'N/A')
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


    # 4. リスク管理とフッター
    regime = best_signal.get('regime', 'N/A')
    
    footer = (
        f"==================================\n"
        f"| 🔍 **市場環境** | **{regime}** 相場 (ADX: {best_signal.get('tech_data', {}).get('adx', 0.0):.2f}) |\n"
        f"| ⚙️ **BOT Ver** | **v17.0.21** - VWAP_FALLBACK_FIX |\n" 
        f"==================================\n"
        f"\n<pre>※ Limit注文は、価格が指定水準に到達した際のみ約定します。DTS戦略では、価格が有利な方向に動いた場合、SLが自動的に追跡され利益を最大化します。</pre>"
    )

    # 全ブロックを結合して返す
    return header + trade_plan + pnl_prediction_block + pivot_pnl_block + analysis_detail + footer


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
            if df_btc['close'].iloc[-1] > df_btc['sma'].iloc[-1]: btc_trend = 1 # Long
            elif df_btc['close'].iloc[-1] < df_btc['sma'].iloc[-1]: btc_trend = -1 # Short
            if len(df_btc) >= 2:
                btc_change = (df_btc['close'].iloc[-1] - df_btc['close'].iloc[-2]) / df_btc['close'].iloc[-2]
    
    if status_eth == "Success":
        df_eth = pd.DataFrame(eth_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df_eth['close'] = pd.to_numeric(df_eth['close'], errors='coerce').astype('float64')
        df_eth['sma'] = ta.sma(df_eth['close'], length=LONG_TERM_SMA_LENGTH)
        df_eth.dropna(subset=['sma'], inplace=True)
        if not df_eth.empty:
            if df_eth['close'].iloc[-1] > df_eth['sma'].iloc[-1]: eth_trend = 1 # Long
            elif df_eth['close'].iloc[-1] < df_eth['sma'].iloc[-1]: eth_trend = -1 # Short
            if len(df_eth) >= 2:
                eth_change = (df_eth['close'].iloc[-1] - df_eth['close'].iloc[-2]) / df_eth['close'].iloc[-2]

    # 2. FGI Proxyの計算 (恐怖指数/市場センチメント)
    sentiment_score = 0.0
    if btc_trend == 1 and eth_trend == 1:
        sentiment_score = 0.07 # 強いリスクオン (ボーナス強化)
    elif btc_trend == 1 or eth_trend == 1:
        sentiment_score = 0.03 # リスクオン傾向
    elif btc_trend == -1 and eth_trend == -1:
        sentiment_score = -0.07 # 強いリスクオフ (恐怖) (ボーナス強化)
    elif btc_trend == -1 or eth_trend == -1:
        sentiment_score = -0.03 # リスクオフ傾向
    
    # 3. BTC Dominance Proxyの計算
    dominance_trend = "Neutral"
    dominance_bias_score = 0.0
    DOM_DIFF_THRESHOLD = 0.002 # 0.2%以上の差でドミナンスに偏りありと判断
    
    if status_btc == "Success" and status_eth == "Success" and len(df_btc) >= 2 and len(df_eth) >= 2:
        if btc_change - eth_change > DOM_DIFF_THRESHOLD: # BTCの上昇/下落がETHより強い -> ドミナンス増 (BTC強、Alt弱)
            dominance_trend = "Increasing"
            dominance_bias_score = -DOMINANCE_BIAS_BONUS_PENALTY # Altcoin Longにペナルティ
        elif eth_change - btc_change > DOM_DIFF_THRESHOLD: # ETHの上昇/下落がBTCより強い -> ドミナンス減 (Alt強、BTC弱)
            dominance_trend = "Decreasing"
            dominance_bias_score = DOMINANCE_BIAS_BONUS_PENALTY # Altcoin Longにボーナス

    return {
        "btc_trend_4h": "Long" if btc_trend == 1 else ("Short" if btc_trend == -1 else "Neutral"),
        "eth_trend_4h": "Long" if eth_trend == 1 else ("Short" if eth_trend == -1 else "Neutral"),
        "sentiment_fgi_proxy": sentiment_score,
        "dominance_trend": dominance_trend,
        "dominance_bias_score": dominance_bias_score
    }

# ====================================================================================
# CORE ANALYSIS LOGIC
# ====================================================================================

# Fibonacci Pivot Point Calculation Utility
def calculate_fib_pivot(df: pd.DataFrame) -> Dict:
    """直近のバーに基づきフィボナッチ・ピボットポイントを計算する (H, L, C, P, R, S)"""
    if len(df) < 2: return {'P': np.nan, 'R1': np.nan, 'S1': np.nan}

    # 最新の完成足を使用 (index -2)
    H = df['high'].iloc[-2] 
    L = df['low'].iloc[-2] 
    C = df['close'].iloc[-2]
    
    P = (H + L + C) / 3 
    
    # フィボナッチ係数
    R1 = P + (H - L) * 0.382
    S1 = P - (H - L) * 0.382
    R2 = P + (H - L) * 0.618
    S2 = P - (H - L) * 0.618
    
    return {'P': P, 'R1': R1, 'R2': R2, 'S1': S1, 'S2': S2}

def analyze_structural_proximity(price: float, pivots: Dict, side: str) -> Tuple[float, float, float]:
    """
    価格とPivotポイントを比較し、構造的なSL/TPを決定し、ボーナススコアを返す (0.07点)
    返り値: (ボーナススコア, 構造的SL, 構造的TP)
    """
    bonus = 0.0
    structural_sl = 0.0
    structural_tp = 0.0
    BONUS_POINT = 0.07
    
    R1 = pivots.get('R1', np.nan)
    S1 = pivots.get('S1', np.nan)
    P = pivots.get('P', np.nan)
    
    if pd.isna(R1) or pd.isna(S1) or pd.isna(P):
        return 0.0, 0.0, 0.0

    # 構造的なSL/TPの採用
    if side == "ロング":
        # 価格がS1とPの間にある場合
        if price > S1 and price < P: 
            # 価格がS1に近いほどボーナス
            if (P - price) / (P - S1) > 0.5:
                bonus = BONUS_POINT
                structural_sl = S1
                structural_tp = P
            
        # 価格がPとR1の間にある場合 (構造的なブレイクアウトの押し目狙い)
        elif price > P and price < R1:
            # 価格がPに近いほどボーナス
            if (R1 - price) / (R1 - P) > 0.5:
                 bonus = BONUS_POINT * 0.5 # 軽めのボーナス
                 structural_sl = P
                 structural_tp = R1


        
    elif side == "ショート":
        # 価格がR1とPの間にある場合
        if price < R1 and price > P: 
            # 価格がR1に近いほどボーナス
            if (price - P) / (R1 - P) > 0.5:
                bonus = BONUS_POINT
                structural_sl = R1
                structural_tp = P
                
        # 価格がPとS1の間にある場合 (構造的なブレイクアウトの戻り売り狙い)
        elif price < P and price > S1:
            # 価格がPに近いほどボーナス
            if (price - S1) / (P - S1) > 0.5:
                 bonus = BONUS_POINT * 0.5 # 軽めのボーナス
                 structural_sl = P
                 structural_tp = S1
            
    return bonus, structural_sl, structural_tp


def calculate_indicators(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """テクニカル指標を計算し、DataFrameに追加する"""
    
    # --- ボラティリティ指標 ---
    df.ta.bbands(length=20, append=True)
    df.ta.atr(length=14, append=True)
    
    # --- モメンタム指標 ---
    df.ta.rsi(length=14, append=True)
    df.ta.macd(fast=12, slow=26, signal=9, append=True)
    df.ta.cci(length=20, append=True)
    
    # --- トレンド指標 ---
    df.ta.adx(length=14, append=True)
    df.ta.vwap(append=True) # pandas_taによるVWAP計算
    
    # --- 長期トレンドの確認 (4h足のみ) ---
    if timeframe == '4h':
        df.ta.sma(length=LONG_TERM_SMA_LENGTH, append=True)
        
    # --- STOCHRSIの追加 ---
    df.ta.stochrsi(append=True)
    
    # 出来高の移動平均線
    df['volume_ma'] = df['volume'].rolling(window=20).mean()

    # --- ADD v17.0.21: VWAPの計算失敗時の手動フォールバック ---
    # pandas_taがカラムを生成しなかった、または全てNaNだった場合
    if 'VWAP' not in df.columns or df['VWAP'].isnull().all():
        logging.warning(f"⚠️ {timeframe} VWAP計算失敗: pandas_taがカラムを生成できませんでした。手動フォールバックを試行します。")
        # 手動 VWAP 計算 (PV = Price * Volume, CUSUM of PV / CUSUM of Volume)
        if df['volume'].sum() > 0:
            df['PV'] = (df['high'] + df['low'] + df['close']) / 3 * df['volume']
            df['VWAP'] = df['PV'].cumsum() / df['volume'].cumsum()
            df.drop(columns=['PV'], inplace=True, errors='ignore')
        else:
            # 出来高がゼロの場合は、VWAP計算は不可能と判断
            logging.warning(f"⚠️ {timeframe} VWAP手動計算失敗: 出来高がゼロのため計算できません。")
            df['VWAP'] = np.nan 
    # ----------------------------------------------------------

    # 必要なコアインジケータのカラム名
    required_cols = ['close', 'RSI_14', 'MACDh_12_26_9', 'ADX_14', 'ATR_14', 'BBL_20_2.0', 'BBU_20_2.0', 'STOCHRSIk_14_14_3_3', 'VWAP', 'volume_ma']
    
    # 欠損しているカラム名を取得
    missing_cols = [col for col in required_cols if col not in df.columns]

    if missing_cols:
        # ログ出力を行い、致命的なエラーを防ぐために空のDataFrameを返す
        logging.error(f"⚠️ 指標計算失敗 (タイムフレーム: {timeframe}): 以下の必要なカラムがありません: {', '.join(missing_cols)}")
        return pd.DataFrame() # 空のDataFrameを返して、`analyze_symbol_async`で'DataShortage'として処理させる

    # 欠損値を含む行を削除 (これにより、後続のロジックでKeyErrorは発生しない)
    return df.dropna(subset=required_cols)


def determine_trend_regime(adx: float, pdi: float, mdi: float) -> str:
    """ADX値と+/-DIに基づき、相場環境 (Regime) を決定する"""
    if adx > ADX_TREND_THRESHOLD:
        if pdi > mdi:
            return "Strong_Uptrend"
        else:
            return "Strong_Downtrend"
    else:
        return "Consolidation"


def score_and_signal(df: pd.DataFrame, symbol: str, timeframe: str, macro_context: Dict) -> Dict:
    """
    テクニカル分析とスコアリングを実行し、シグナルを生成する
    """
    
    if df.empty or len(df) < 30:
        return {'symbol': symbol, 'timeframe': timeframe, 'side': 'DataShortage', 'score': 0.0, 'signals': [], 'rr_ratio': 0.0}

    # 最新の指標値を取得
    last = df.iloc[-1]
    
    price = last['close']
    rsi = last['RSI_14']
    macd_hist = last['MACDh_12_26_9']
    adx = last['ADX_14']
    pdi = last['PDI_14']
    mdi = last['MDI_14']
    cci = last['CCI_20']
    atr_value = last['ATR_14']
    bb_low = last['BBL_20_2.0']
    bb_high = last['BBU_20_2.0']
    vwap = last['VWAP']
    stochrsik = last['STOCHRSIk_14_14_3_3']
    stochrsid = last['STOCHRSId_14_14_3_3']
    
    # 1. 相場環境 (Regime) の決定
    regime = determine_trend_regime(adx, pdi, mdi)
    
    # 2. 初期シグナルとベーススコアの設定
    signal_side = "Neutral"
    base_score = BASE_SCORE # 0.40
    score = base_score
    is_long = False
    is_short = False
    
    # 3. 構造分析 (Pivot)
    pivots = calculate_fib_pivot(df)
    
    # ----------------------------------------------------
    # Long & Short シグナルの判定
    # ----------------------------------------------------
    
    # Long判定
    if rsi < RSI_OVERSOLD and cci < -100 and macd_hist > 0:
        is_long = True
    elif rsi < RSI_MOMENTUM_LOW and macd_hist > 0 and (pdi > mdi or adx < ADX_TREND_THRESHOLD): # 押し目・レンジ下限
         is_long = True
         
    # Short判定
    if rsi > RSI_OVERBOUGHT and cci > 100 and macd_hist < 0:
        is_short = True
    elif rsi > RSI_MOMENTUM_HIGH and macd_hist < 0 and (mdi > pdi or adx < ADX_TREND_THRESHOLD): # 戻り売り・レンジ上限
        is_short = True
        
    # 4. スコアリングとエントリー/ストップロスの計算
    entry_price = price # 初期設定
    sl_price = 0.0
    tp_price = 0.0
    rr_ratio = 0.0
    entry_type = "Market"
    
    structural_sl_used = False
    
    if is_long or is_short:
        
        # --- 基本点 (RSI/CCI) の加算 ---
        if is_long:
            score += 0.08 * (1 - rsi / 50) 
        elif is_short:
            score += 0.08 * (rsi / 50 - 1)
            
        # --- MACDモメンタム点の加算 ---
        score += min(0.10, abs(macd_hist) * 20) 
        
        # --- ADX点の加算 ---
        if regime.startswith('Strong'):
             score += min(0.05, (adx - ADX_TREND_THRESHOLD) / 10 * 0.05) if adx > ADX_TREND_THRESHOLD else 0.0
        else: # Consolidation
             score += 0.03 
             
        # --- 構造的S/Rのボーナス ---
        structural_bonus, structural_sl_candidate, structural_tp_candidate = analyze_structural_proximity(price, pivots, "ロング" if is_long else "ショート")
        score += structural_bonus
        
        # --- Entry/SL/TPの決定 ---
        
        # SLの初期位置 (ATR基準)
        atr_sl_width = atr_value * ATR_TRAIL_MULTIPLIER
        
        if is_long:
            signal_side = "ロング"
            
            # エントリーはS1/P付近を狙う (Limit)
            entry_target = pivots['S1'] if structural_bonus > 0 else (price * 0.999) # 構造的なLimitを優先
            entry_price = (price + entry_target) / 2 if entry_target > 0 else price
            entry_type = "Limit" 
            
            # SLの決定
            if structural_sl_candidate > 0 and structural_sl_candidate < entry_price:
                sl_price = structural_sl_candidate - (0.5 * atr_value) # 構造的SLにATRバッファ
                structural_sl_used = True
            else:
                sl_price = entry_price - atr_sl_width 
            
            # TPは遠くのR1またはRRR 5.0 を満たす位置 (表示用)
            tp_price = entry_price + (abs(entry_price - sl_price) * DTS_RRR_DISPLAY)
            
        elif is_short:
            signal_side = "ショート"
            
            # エントリーはR1/P付近を狙う (Limit)
            entry_target = pivots['R1'] if structural_bonus > 0 else (price * 1.001) # 構造的なLimitを優先
            entry_price = (price + entry_target) / 2 if entry_target > 0 else price
            entry_type = "Limit" 
            
            # SLの決定
            if structural_sl_candidate > 0 and structural_sl_candidate > entry_price:
                sl_price = structural_sl_candidate + (0.5 * atr_value) # 構造的SLにATRバッファ
                structural_sl_used = True
            else:
                sl_price = entry_price + atr_sl_width 
            
            # TPは遠くのS1またはRRR 5.0 を満たす位置 (表示用)
            tp_price = entry_price - (abs(entry_price - sl_price) * DTS_RRR_DISPLAY)
        
        
        # 5. リスクリワード比率 (RRR) の計算
        sl_width = abs(entry_price - sl_price)
        tp_width = abs(tp_price - entry_price)
        rr_ratio = tp_width / sl_width if sl_width > 0 else 0.0
        
        
        # 6. ペナルティの適用
        
        # A. 長期トレンドとの逆張りペナルティ (4h足 SMA 50)
        long_term_reversal_penalty = False
        long_term_reversal_penalty_value = 0.0
        if timeframe != '4h' and last.get('sma') is not None:
            long_term_trend = 1 if last['close'] > last['sma'] else -1
            if (is_long and long_term_trend == -1) or (is_short and long_term_trend == 1):
                score -= LONG_TERM_REVERSAL_PENALTY
                long_term_reversal_penalty = True
                long_term_reversal_penalty_value = LONG_TERM_REVERSAL_PENALTY
        
        # B. MACDクロスによるモメンタムのペナルティ
        macd_cross_valid = True
        macd_cross_penalty_value = 0.0
        if (is_long and macd_hist < 0) or (is_short and macd_hist > 0):
            score -= MACD_CROSS_PENALTY
            macd_cross_valid = False
            macd_cross_penalty_value = MACD_CROSS_PENALTY
            
        # C. VWAPの整合性チェック
        vwap_consistent = (is_long and price > vwap) or (is_short and price < vwap)
        if not vwap_consistent:
            score -= 0.10
        
        # D. ボラティリティのペナルティ 
        bb_width_percent = (bb_high - bb_low) / price * 100
        if bb_width_percent < VOLATILITY_BB_PENALTY_THRESHOLD:
            score -= 0.10
            
        # E. STOCHRSIによる過熱感ペナルティ
        stoch_filter_penalty = 0.0
        if is_long and stochrsik > 80 and stochrsid > 80:
             stoch_filter_penalty = 0.10
             score -= stoch_filter_penalty
        elif is_short and stochrsik < 20 and stochrsid < 20:
             stoch_filter_penalty = 0.10
             score -= stoch_filter_penalty

        # 7. ボーナスの適用
        
        # A. 出来高確証ボーナス (Volume Confirmation)
        volume_ratio = last['volume'] / last['volume_ma']
        volume_confirmation_bonus = 0.0
        if volume_ratio > VOLUME_CONFIRMATION_MULTIPLIER:
            volume_confirmation_bonus = 0.05
            score += volume_confirmation_bonus
            
        # B. 資金調達率 (Funding Rate) のバイアスボーナス/ペナルティ
        funding_rate_val = last['funding_rate']
        funding_rate_bonus_value = 0.0
        if funding_rate_val > FUNDING_RATE_THRESHOLD: 
            if is_long:
                funding_rate_bonus_value = FUNDING_RATE_BONUS_PENALTY
            elif is_short:
                funding_rate_bonus_value = -FUNDING_RATE_BONUS_PENALTY
        elif funding_rate_val < -FUNDING_RATE_THRESHOLD: 
            if is_short:
                funding_rate_bonus_value = FUNDING_RATE_BONUS_PENALTY
            elif is_long:
                funding_rate_bonus_value = -FUNDING_RATE_BONUS_PENALTY
        score += funding_rate_bonus_value
        
        # C. BTCドミナンスのバイアスボーナス/ペナルティ (Altcoinのみ)
        dominance_bias_bonus_value = 0.0
        if symbol != 'BTC-USDT' and macro_context.get('dominance_bias_score') != 0.0:
            if macro_context['dominance_trend'] == 'Decreasing': 
                if is_long:
                    dominance_bias_bonus_value = abs(macro_context['dominance_bias_score']) 
                elif is_short:
                    dominance_bias_bonus_value = -abs(macro_context['dominance_bias_score']) 
            elif macro_context['dominance_trend'] == 'Increasing': 
                if is_long:
                    dominance_bias_bonus_value = -abs(macro_context['dominance_bias_score']) 
                elif is_short:
                    dominance_bias_bonus_value = abs(macro_context['dominance_bias_score']) 
        score += dominance_bias_bonus_value

        
        # 8. スコアを 0.0-1.0 に収める
        score = max(0.0, min(1.0, score))
        
        return {
            'symbol': symbol,
            'timeframe': timeframe,
            'side': signal_side,
            'score': score,
            'entry': entry_price,
            'sl': sl_price,
            'tp1': tp_price,
            'rr_ratio': rr_ratio,
            'entry_type': entry_type,
            'regime': regime,
            'macro_context': macro_context,
            'tech_data': {
                'price': price,
                'rsi': rsi,
                'macd_hist': macd_hist,
                'adx': adx,
                'pdi': pdi,
                'mdi': mdi,
                'cci': cci,
                'atr_value': atr_value,
                'long_term_trend': "Long" if (timeframe == '4h' and last.get('sma') is not None and last['close'] > last['sma']) else ("Short" if (timeframe == '4h' and last.get('sma') is not None and last['close'] < last['sma']) else "N/A"),
                'long_term_reversal_penalty': long_term_reversal_penalty,
                'long_term_reversal_penalty_value': long_term_reversal_penalty_value,
                'macd_cross_valid': macd_cross_valid,
                'macd_cross_penalty_value': macd_cross_penalty_value,
                'vwap_consistent': vwap_consistent,
                'structural_pivot_bonus': structural_bonus,
                'structural_sl_used': structural_sl_used,
                'volume_confirmation_bonus': volume_confirmation_bonus,
                'volume_ratio': volume_ratio,
                'funding_rate_value': funding_rate_val,
                'funding_rate_bonus_value': funding_rate_bonus_value,
                'dominance_trend': macro_context.get('dominance_trend', 'N/A'),
                'dominance_bias_bonus_value': dominance_bias_bonus_value,
                'stoch_filter_penalty': stoch_filter_penalty,
                'pivot_points': pivots # Pivot Pointsを格納
            }
        }
        
    return {'symbol': symbol, 'timeframe': timeframe, 'side': signal_side, 'score': score, 'signals': [], 'rr_ratio': 0.0, 'regime': regime, 'macro_context': macro_context}


async def analyze_symbol_async(symbol: str, macro_context: Dict) -> Dict:
    """単一のシンボルについて3つの時間足で非同期に分析を行う"""
    
    # 1. 資金調達率を取得
    funding_rate = await fetch_funding_rate(symbol)
    
    # 2. 3つの時間足のOHLCVデータを非同期で取得
    timeframes = ['15m', '1h', '4h']
    tasks = [fetch_ohlcv_with_fallback(CCXT_CLIENT_NAME, symbol, tf) for tf in timeframes]
    results = await asyncio.gather(*tasks)
    
    combined_signals = []
    
    for (ohlcv, status, client), timeframe in zip(results, timeframes):
        if status == "Success":
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            
            # --- FIX v17.0.17: VWAPのエラー/警告解消のためDatatimeIndexを設定 ---
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)
            # -----------------------------------------------------------------
            
            df['close'] = pd.to_numeric(df['close'], errors='coerce').astype('float64')
            df['open'] = pd.to_numeric(df['open'], errors='coerce').astype('float64')
            df['high'] = pd.to_numeric(df['high'], errors='coerce').astype('float64')
            df['low'] = pd.to_numeric(df['low'], errors='coerce').astype('float64')
            df['volume'] = pd.to_numeric(df['volume'], errors='coerce').astype('float64')
            
            # --- FIX v17.0.19: 不完全なOHLCVデータ行を削除し、pandas_taの計算失敗を防ぐ ---
            df.dropna(subset=['open', 'high', 'low', 'close', 'volume'], inplace=True)
            # ---------------------------------------------------------------------------------

            # --- FIX v17.0.20: データフレームが短すぎる場合の早期退出 ---
            current_len = len(df)
            if current_len < MINIMUM_DATAFRAME_LENGTH: 
                logging.warning(f"⚠️ {symbol} [{timeframe}] クリーンアップ後のデータが短すぎます ({current_len}行)。DataShortageとして処理します。")
                combined_signals.append({'symbol': symbol, 'timeframe': timeframe, 'side': 'DataShortage', 'score': 0.0, 'signals': [], 'rr_ratio': 0.0})
                continue
            
            # --- ADD v17.0.21: Post-Cleanup DataFrame Length Log (for troubleshooting) ---
            # NOTE: デフォルトのロギングレベルがINFOのため、このログを確認するにはロギング設定を変更する必要があります。
            logging.debug(f"ℹ️ {symbol} [{timeframe}] クリーンアップ後のデータ長: {current_len}行。")
            # ---------------------------------------------------------------------------------

            df = calculate_indicators(df, timeframe)
            
            # calculate_indicatorsが空のDataFrameを返した場合もDataShortageとして処理
            if df.empty:
                combined_signals.append({'symbol': symbol, 'timeframe': timeframe, 'side': 'DataShortage', 'score': 0.0, 'signals': [], 'rr_ratio': 0.0})
                continue

            # 資金調達率を追加
            df['funding_rate'] = funding_rate
            
            signal_data = score_and_signal(df, symbol, timeframe, macro_context)
            combined_signals.append(signal_data)
        else:
            combined_signals.append({'symbol': symbol, 'timeframe': timeframe, 'side': status, 'score': 0.0, 'signals': [], 'rr_ratio': 0.0})

    # 3. 3つの時間足のシグナルを統合してスコアを決定
    valid_signals = [s for s in combined_signals if s.get('side') not in ["DataShortage", "ExchangeError", "Neutral"]]
    
    if not valid_signals:
        return {'symbol': symbol, 'score': 0.0, 'signals': combined_signals, 'main_side': 'Neutral', 'rr_ratio': 0.0}

    # 最もスコアの高いシグナルを採用
    best_signal = max(valid_signals, key=lambda s: s.get('score', 0.0))
    main_side = best_signal['side']
    
    # 4. 統合スコアの計算
    avg_score = sum(s.get('score', 0.0) for s in valid_signals) / len(valid_signals)
    integrated_score = (avg_score * 0.4) + (best_signal.get('score', 0.0) * 0.6) # ベストスコアを重視
    
    # 5. 最終的な統合シグナルを返す
    return {
        'symbol': symbol,
        'score': integrated_score,
        'signals': combined_signals,
        'main_side': main_side,
        'rr_ratio': best_signal.get('rr_ratio', 0.0) # <-- FIX v17.0.19: .get()を使用してKeyErrorを回避
    }


def get_all_sorted_signals(analysis_results: List[Dict]) -> List[Dict]:
    """
    全ての分析結果から、有効な取引シグナルを抽出し、スコア順にソートする
    """
    
    # スコアが0より大きく、メインシグナルがNeutralでないものをフィルタリング
    valid_signals = [
        sig for sig in analysis_results 
        if sig.get('score', 0.0) > 0.0 and sig.get('main_side') not in ['Neutral', 'DataShortage', 'ExchangeError']
    ]
    
    # 統合スコア (score) で降順にソート
    sorted_signals = sorted(valid_signals, key=lambda s: s.get('score', 0.0), reverse=True)
    
    return sorted_signals


# ====================================================================================
# MAIN LOOP
# ====================================================================================

async def main_loop():
    """メインのデータ取得、分析、通知ループ"""
    global LAST_ANALYSIS_SIGNALS, LAST_SUCCESS_TIME, TRADE_NOTIFIED_SYMBOLS
    
    while True:
        try:
            now_jst = datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')
            logging.info(f"--- 🔄 Apex BOT 分析サイクル開始: {now_jst} ---")
            
            # 1. 銘柄リストの更新とマクロコンテキストの取得
            await update_symbols_by_volume()
            macro_context = await get_crypto_macro_context()
            
            logging.info(f"マクロ市場コンテキスト: Sentiment={macro_context['sentiment_fgi_proxy']:.2f}, Dominance={macro_context['dominance_trend']}")

            # 2. 全ての銘柄の分析を非同期で実行
            tasks = [analyze_symbol_async(symbol, macro_context) for symbol in CURRENT_MONITOR_SYMBOLS]
            analysis_results = await asyncio.gather(*tasks)
            
            # 3. 分析結果のフィルタリングとソート
            all_sorted_signals = get_all_sorted_signals(analysis_results)
            LAST_ANALYSIS_SIGNALS = all_sorted_signals
            
            logging.info(f"合計 {len(analysis_results)} 銘柄を分析しました。有効なシグナル: {len(all_sorted_signals)} 件。")
            
            # 4. Telegram通知ロジック (クールダウン無効、常にベスト1を通知)
            notification_count = 0
            
            if all_sorted_signals:
                # 最もスコアの高いシグナル (Rank 1) を取得
                best_signal_overall = all_sorted_signals[0]
                symbol = best_signal_overall['symbol']
                score_100 = best_signal_overall['score'] * 100
                
                # 閾値チェックやクールダウンチェックを一切行わず、常に通知する
                message = format_integrated_analysis_message(symbol, best_signal_overall['signals'], 1)

                if message:
                    send_telegram_html(message)
                    
                    # クールダウンが無効（1秒）であるため、TRADE_NOTIFIED_SYMBOLSは事実上意味を成さないが、ログ/ステータス管理のために更新
                    TRADE_NOTIFIED_SYMBOLS[symbol] = time.time() 
                    notification_count += 1
                    
                    # ログの出力は古いSIGNAL_THRESHOLDを参照するが、ロジック上は常に通知される
                    if best_signal_overall['score'] * 100 < 75.0: 
                         logging.info(f"Telegram通知を 1 件送信しました。(TOPシグナル: {symbol.replace('-', '/')} - スコア不成立 {score_100:.2f}点 - クールダウン無視)")
                    else:
                         logging.info(f"Telegram通知を 1 件送信しました。(TOPシグナル: {symbol.replace('-', '/')} - スコア成立 {score_100:.2f}点 - クールダウン無視)")
            
            # 5. ループ処理の完了と待機
            LAST_SUCCESS_TIME = time.time()
            logging.info(f"今回の分析サイクルは完了しました。通知件数: {notification_count} 件。")
            
            time_taken = time.time() - LAST_SUCCESS_TIME 
            sleep_time = max(1, LOOP_INTERVAL - time_taken)
            logging.info(f"次のサイクルまで {sleep_time:.0f} 秒待機します...")
            await asyncio.sleep(sleep_time)

        except Exception as e:
            error_name = type(e).__name__
            if "KeyboardInterrupt" in error_name:
                 pass 
            
            logging.error(f"メインループで致命的なエラー: {error_name}")
            await asyncio.sleep(60)


# ====================================================================================
# FASTAPI SETUP
# ====================================================================================

app = FastAPI(title="Apex BOT API", version="v17.0.21 - VWAP_FALLBACK_FIX")

@app.on_event("startup")
async def startup_event():
    logging.info("🚀 Apex BOT v17.0.21 Startup initializing...") 
    await initialize_ccxt_client()
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
        "bot_version": "v17.0.21 - VWAP_FALLBACK_FIX",
        "last_success_time_utc": datetime.fromtimestamp(LAST_SUCCESS_TIME, tz=timezone.utc).isoformat() if LAST_SUCCESS_TIME else "N/A",
        "current_client": CCXT_CLIENT_NAME,
        "monitoring_symbols": len(CURRENT_MONITOR_SYMBOLS),
        "last_signals_count": len(LAST_ANALYSIS_SIGNALS)
    }
    return JSONResponse(content=status_msg)

@app.head("/")
@app.get("/")
def home_view():
    return JSONResponse(content={"message": "Apex BOT is running on v17.0.21."})

if __name__ == "__main__":
    # 環境変数からポート番号を取得。デフォルトは8000
    port = int(os.environ.get("PORT", 8000))
    # 開発環境での実行
    uvicorn.run(app, host="0.0.0.0", port=port)
