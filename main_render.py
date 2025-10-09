# ====================================================================================
# Apex BOT v17.0.5 - Fix Fatal IndexError in analyze_single_timeframe (Post-Processing Check)
# - FIX: analyze_single_timeframe 関数内で、テクニカル計算後に再度 DataFrame のサイズをチェック (len(df) < 2) し、iloc[-1] や iloc[-2] アクセスによる IndexError を完全に防止。
# - FIX: fetch_ohlcv_data のログメッセージを修正。
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
POSITION_CAPITAL = 1000.0           # 1x想定のポジションサイズ (USD)

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

def format_pnl_utility_telegram(pnl_usd: float) -> str:
    """損益額をTelegram表示用に整形し、色付けする"""
    if pnl_usd > 0.0001:
        return f"<ins>+${pnl_usd:,.2f}</ins> 🟢"
    elif pnl_usd < -0.0001:
        # マイナス記号を付けて、赤色にする
        return f"<ins>${pnl_usd:,.2f}</ins> 🔴" 
    return f"+$0.00 🟡"

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

def calculate_pnl_at_pivot(target_price: float, entry: float, side_long: bool, capital: float) -> float:
    """Pivot価格到達時の損益を計算する (1x想定)"""
    if target_price <= 0 or entry <= 0: return 0.0
    
    # 数量 = 資本 / エントリー価格
    quantity = capital / entry
    
    # 損益 = 数量 * (目標価格 - エントリー価格)
    pnl = quantity * (target_price - entry)
    
    # ショートの場合は符号を反転させる (目標価格がエントリー価格より低いと利益になるため)
    if not side_long:
        pnl = -pnl
        
    return pnl


def format_integrated_analysis_message(symbol: str, signals: List[Dict], rank: int) -> str:
    """
    3つの時間軸の分析結果を統合し、ログメッセージの形式に整形する (v17.0.5対応)
    """
    global POSITION_CAPITAL
    
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
    sl_width_calculated = abs(entry_price - sl_price)
    
    # NEW: $1000 ポジションに基づくP&L計算 (1xレバレッジ)
    is_long = (side == "ロング")
    if entry_price > 0:
        # 数量 = 資本 / エントリー価格
        quantity = POSITION_CAPITAL / entry_price
        
        # SL/TPの絶対損益額
        sl_risk_usd_abs = quantity * sl_width_calculated 
        tp_gain_usd_abs = quantity * abs(entry_price - tp_price) 
        
        # 損益率
        sl_risk_percent = (sl_risk_usd_abs / POSITION_CAPITAL) * 100
        tp_gain_percent = (tp_gain_usd_abs / POSITION_CAPITAL) * 100
        
    else:
        sl_risk_usd_abs = 0.0
        tp_gain_usd_abs = 0.0
        sl_risk_percent = 0.0
        tp_gain_percent = 0.0

    sl_loss_usd = sl_risk_usd_abs
    tp_gain_usd = tp_gain_usd_abs

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
        f"{rank_header} 🔥 {strength} 発生！ - {direction_emoji}{market_sentiment_str}\n" 
        f"==================================\n"
        f"| 🎯 **予測勝率** | **<ins>{win_rate:.1f}%</ins>** | **条件極めて良好** |\n"
        f"| 💯 **分析スコア** | <b>{score_100:.2f} / 100.00 点</b> (ベース: {timeframe}足) |\n" 
        f"| 💰 **予想損益** | **<ins>損益比 1:{rr_ratio:.2f}</ins>** (損失: ${sl_loss_usd:,.0f} / 利益: ${tp_gain_usd:,.0f}+) |\n"
        f"| ⏰ **決済戦略** | **{exit_type_str}** (目標RRR: 1:{rr_ratio:.2f}+) |\n" 
        f"| ⏳ **TP到達目安** | **{time_to_tp}** | (変動する可能性があります) |\n"
        f"==================================\n"
    )

    sl_source_str = "ATR基準"
    if best_signal.get('tech_data', {}).get('structural_sl_used', False):
        sl_source_str = "構造的 (Pivot) + **0.5 ATR バッファ**" 
        
    # 取引計画の表示をDTSに合わせて変更
    trade_plan = (
        f"**🎯 推奨取引計画 (Dynamic Trailing Stop & Structural SL)**\n"
        f"----------------------------------\n"
        f"| 指標 | 価格 (USD) | 備考 |\n"
        f"| :--- | :--- | :--- |\n"
        f"| 💰 現在価格 | <code>${format_price_utility(price, symbol)}</code> | 参照価格 |\n"
        f"| ➡️ **Entry ({entry_type})** | <code>${format_price_utility(entry_price, symbol)}</code> | {side}ポジション (**<ins>底/天井を狙う Limit 注文</ins>**) |\n" 
        f"| 📉 **Risk (SL幅)** | ${format_price_utility(sl_width_calculated, symbol)} | **初動リスク** (ATR x {ATR_TRAIL_MULTIPLIER:.1f}) |\n"
        f"| 🟢 TP 目標 | <code>${format_price_utility(tp_price, symbol)}</code> | **動的決済** (DTSにより利益最大化) |\n" 
        f"| ❌ SL 位置 | <code>${format_price_utility(sl_price, symbol)}</code> | 損切 ({sl_source_str} / **初期追跡ストップ**) |\n"
        f"----------------------------------\n"
    )

    # NEW: SL/TP 到達時のP&Lブロック (1000 USD ポジション)
    pnl_block = (
        f"\n**📈 損益結果 ({POSITION_CAPITAL:,.0f} USD ポジションの場合)**\n"
        f"----------------------------------\n"
        f"| 項目 | **損益額 (USD)** | 損益率 (対ポジションサイズ) |\n"
        f"| :--- | :--- | :--- |\n"
        f"| ❌ SL実行時 | **{format_pnl_utility_telegram(-sl_risk_usd_abs)}** | {sl_risk_percent:.2f}% |\n" 
        f"| 🟢 TP目標時 | **{format_pnl_utility_telegram(tp_gain_usd_abs)}** | {tp_gain_percent:.2f}% |\n"
        f"----------------------------------\n"
    )
    
    # NEW: Pivot S/R 到達時のP&Lブロック (1000 USD ポジション)
    pivot_points = best_signal.get('tech_data', {}).get('pivot_points', {})
    
    pivot_pnl_block = ""
    pivot_r1 = pivot_points.get('r1', 0.0)
    pivot_r2 = pivot_points.get('r2', 0.0)
    pivot_s1 = pivot_points.get('s1', 0.0)
    pivot_s2 = pivot_points.get('s2', 0.0)

    if pivot_r1 > 0 and entry_price > 0 and side in ["ロング", "ショート"]:
        
        # Long/Shortに応じてP&Lを計算
        is_long = (side == "ロング")
        pnl_r1 = calculate_pnl_at_pivot(pivot_r1, entry_price, is_long, POSITION_CAPITAL)
        pnl_r2 = calculate_pnl_at_pivot(pivot_r2, entry_price, is_long, POSITION_CAPITAL)
        pnl_s1 = calculate_pnl_at_pivot(pivot_s1, entry_price, is_long, POSITION_CAPITAL)
        pnl_s2 = calculate_pnl_at_pivot(pivot_s2, entry_price, is_long, POSITION_CAPITAL)
        
        # 損益ブロックの構築
        pivot_pnl_block = (
            f"\n**🧮 ${POSITION_CAPITAL:,.0f} ポジションの到達損益 (Pivot S/R) [1x想定]**\n"
            f"----------------------------------\n"
            f"| 目標レベル | **価格 (USD)** | 損益 (概算) |\n"
            f"| :--- | :--- | :--- |\n"
            f"| 📈 **抵抗線 R1** | <code>${format_price_utility(pivot_r1, symbol)}</code> | {format_pnl_utility_telegram(pnl_r1)} |\n"
            f"| 🚨 **抵抗線 R2** | <code>${format_price_utility(pivot_r2, symbol)}</code> | {format_pnl_utility_telegram(pnl_r2)} |\n"
            f"| 📉 **支持線 S1** | <code>${format_price_utility(pivot_s1, symbol)}</code> | {format_pnl_utility_telegram(pnl_s1)} |\n"
            f"| 🚀 **支持線 S2** | <code>${format_price_utility(pivot_s2, symbol)}</code> | {format_pnl_utility_telegram(pnl_s2)} |\n"
            f"----------------------------------\n"
        )
    
    # NEW: 構造的S/R候補ブロック (P&Lブロックで全て表示されるため、このブロックは省略される可能性あり)
    sr_info = ""
    if pivot_points and not pivot_pnl_block: # P&Lブロックがない場合にのみS/R候補を表示
        r1 = format_price_utility(pivot_r1, symbol)
        r2 = format_price_utility(pivot_r2, symbol)
        s1 = format_price_utility(pivot_s1, symbol)
        s2 = format_price_utility(pivot_s2, symbol)
        pp = format_price_utility(pivot_points.get('pp', 0.0), symbol)
        
        sr_info = (
            f"\n**🧱 構造的S/R候補 (日足)**\n"
            f"----------------------------------\n"
            f"| 候補 | 価格 (USD) | 種類 |\n"
            f"| :--- | :--- | :--- |\n"
            f"| 🛡️ S2 / S1 | <code>${s2}</code> / <code>${s1}</code> | 主要な**支持 (Support)** 候補 |\n"
            f"| 🟡 PP | <code>${pp}</code> | ピボットポイント |\n"
            f"| ⚔️ R1 / R2 | <code>${r2}</code> / <code>${r1}</code> | 主要な**抵抗 (Resistance)** 候補 |\n" # R1/R2の順番を修正
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
                # FIX: regimeをtech_dataから取得
                regime = best_signal.get('tech_data', {}).get('regime', 'N/A')
                
                # ADX/Regime
                analysis_detail += f" └ **ADX/Regime**: {tech_data.get('adx', 0.0):.2f} ({regime})\n"
                # RSI/MACDH/CCI/STOCH
                analysis_detail += f" └ **RSI/MACDH/CCI**: {tech_data.get('rsi', 0.0):.2f} / {tech_data.get('macd_hist', 0.0):.4f} / {tech_data.get('cci', 0.0):.2f}\n"

                # NEW: 構造的S/Rボーナスのハイライト
                pivot_bonus = tech_data.get('structural_pivot_bonus', 0.0)
                pivot_status = "❌ 構造確証に至らず"
                if pivot_bonus > 0:
                     pivot_status = f"✅ **構造的S/Rボーナス** (+{pivot_bonus * 100:.1f}点)"
                elif pivot_bonus < 0:
                    pivot_status = "❌ **エントリー逆行ペナルティ**"
                
                # NEW: 出来高ボーナスのハイライト
                volume_bonus = tech_data.get('volume_confirmation_bonus', 0.0)
                volume_status = "❌ 出来高確証に至らず"
                if volume_bonus > 0:
                    volume_status = f"✅ **出来高確証ボーナス** (+{volume_bonus * 100:.1f}点)"

                analysis_detail += f" └ **S/R & Volume**: {pivot_status} / {volume_status}\n"

    # ----------------------------------------------------
    # 3. マクロコンテキスト
    # ----------------------------------------------------
    macro_context = best_signal.get('macro_context', {})
    
    btc_trend = macro_context.get('btc_trend_4h', 'N/A')
    btc_change = macro_context.get('btc_change_24h', 0.0)
    fgi_proxy = macro_context.get('sentiment_fgi_proxy', 0.0) * 100 # %表示に変換
    dominance_trend = macro_context.get('dominance_trend', 'N/A')
    funding_rate_bias = macro_context.get('funding_rate_bias', 0.0) * 100 # %表示に変換

    macro_detail = (
        f"\n**🌐 マクロコンテキスト**\n"
        f"----------------------------------\n"
        f"| BTC 4h トレンド | **{btc_trend}** |\n"
        f"| BTC 24h 変動率 | **{btc_change:+.2f}%** |\n"
        f"| F&G インデックス | **{fgi_proxy:+.1f}%** (Proxy) |\n"
        f"| BTC Dominance | **{dominance_trend}** (Altスコア補正: {macro_context.get('dominance_bias_value', 0.0) * 100:+.2f}点)|\n"
        f"| Funding Rate Bias | **{funding_rate_bias:+.2f}%** (スコア補正: {macro_context.get('funding_rate_penalty_value', 0.0) * 100:+.2f}点)|\n"
        f"----------------------------------\n"
    )

    # 最終的なメッセージ結合
    final_message = f"{header}\n{trade_plan}\n{pnl_block}\n{pivot_pnl_block}{sr_info}{analysis_detail}\n{macro_detail}"
    
    return final_message.replace("...", "") # 省略記号の除去

# ====================================================================================
# OHLCV DATA & TECHNICAL ANALYSIS
# ====================================================================================

async def initialize_ccxt_client(client_name: str) -> Optional[ccxt_async.Exchange]:
    """CCXTクライアントを初期化する"""
    global CCXT_CLIENT_NAME, EXCHANGE_CLIENT
    
    # 既に初期化されていれば、それを返す
    if EXCHANGE_CLIENT:
        return EXCHANGE_CLIENT
        
    try:
        # ccxtのクラス名を取得 (例: 'okx')
        exchange_class = getattr(ccxt_async, client_name.lower())
        
        # OKX専用の設定 (デリバティブ/スワップ取引)
        if client_name.lower() == 'okx':
            client = exchange_class({
                'options': {
                    'defaultType': 'swap', # 先物/スワップを使用
                }
            })
        else:
            client = exchange_class()

        await client.load_markets()
        
        # APIキー/シークレットがあれば設定
        api_key = os.environ.get(f'{client_name.upper()}_API_KEY')
        secret = os.environ.get(f'{client_name.upper()}_SECRET')
        password = os.environ.get(f'{client_name.upper()}_PASSWORD') # OKX用
        
        if api_key and secret:
            client.apiKey = api_key
            client.secret = secret
            if password:
                client.password = password
            
            logging.info(f"CCXTクライアントを初期化しました ({client_name} - リアル接続, Default: Swap)")
        else:
            logging.info(f"CCXTクライアントを初期化しました ({client_name} - ゲスト接続, Default: Swap)")

        EXCHANGE_CLIENT = client
        return client

    except (AttributeError, ccxt.ExchangeNotAvailable, ccxt.NetworkError) as e:
        logging.error(f"CCXTクライアントの初期化に失敗: {e}")
        return None

async def fetch_ohlcv_data(symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
    """指定されたシンボルと時間軸のOHLCVデータを取得する"""
    global EXCHANGE_CLIENT
    
    if EXCHANGE_CLIENT is None:
        logging.error("OHLCVデータ取得失敗: CCXTクライアントが初期化されていません。")
        return None
        
    try:
        # CCXTのシンボル形式に変換
        ccxt_symbol = symbol.replace('-', '/')
        
        # msecsで取得
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(ccxt_symbol, timeframe, limit=limit)
        
        if not ohlcv or len(ohlcv) < limit:
            # v17.0.5 FIX: ログメッセージを明確化
            logging.warning(f"データ不足: {ccxt_symbol} {timeframe} のデータが {len(ohlcv) if ohlcv else 0}/{limit} しかなく、分析要件を満たしません。")
            return None 

        df = pd.DataFrame(ohlcv, columns=['timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        
        return df
        
    except (ccxt.ExchangeError, ccxt.NetworkError, ccxt.RequestTimeout) as e:
        logging.error(f"OHLCVデータ取得中にエラーが発生しました ({symbol} {timeframe}): {e}")
        return None
    except Exception as e:
        logging.error(f"予期せぬエラーが発生しました ({symbol} {timeframe}): {e}")
        return None

def calculate_technical_indicators(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """
    Pandas TAを使用してテクニカル指標を計算し、DataFrameに結合する
    """
    if df.empty:
        return df

    # --- 1. ボラティリティ (ATR, Bollinger Bands) ---
    df.ta.atr(append=True, length=14)
    df.ta.bbands(length=20, append=True)
    df['BBW'] = df['BBP_20_2.0'].apply(lambda x: x if not np.isnan(x) else 0.0) # BBPがNaNなら0.0

    # --- 2. モメンタム (RSI, MACD, Stochastic RSI) ---
    df.ta.rsi(length=14, append=True)
    df.ta.macd(fast=12, slow=26, signal=9, append=True)
    df.ta.stochrsi(append=True)
    df.ta.cci(length=20, append=True)
    
    # --- 3. トレンド (ADX, SMA) ---
    df.ta.adx(length=14, append=True)
    df.ta.sma(length=LONG_TERM_SMA_LENGTH, append=True, close='Close') # Long-term SMA (50)
    
    # --- 4. VWAP ---
    df.ta.ema(length=20, append=True, close='Close', col_names=('VWAP_Proxy',)) 

    # --- 5. Pivot Points ---
    # ta.pivot_pointsは与えられたデータセットの終了時点のPivotを計算するため、最新の行に値をセット
    # データが少ない場合はNaNになるか、計算されないが、df.iloc[-40:]はIndexErrorを起こさない
    if len(df) >= 20: # 最低限のデータがあることを確認
        pivot_df = ta.pivot_points(df.iloc[-40:], method='standard', append=False) 
        
        if not pivot_df.empty:
            last_pivot = pivot_df.iloc[-1].to_dict()
            for col in pivot_df.columns:
                df[col] = np.nan 
                df.loc[df.index[-1], col] = last_pivot.get(col, np.nan)
            
    # --- 6. Regime Filter (Market Regime) ---
    if len(df) >= 50: # Regime計算に必要な最低限のデータがあることを確認
        regime_df = ta.regime(df.iloc[-50:], append=False)
        if not regime_df.empty:
            df['REGIME'] = np.nan 
            df.loc[df.index[-1], 'REGIME'] = regime_df.iloc[-1].get('REGIME', np.nan)

    return df

def get_pivot_points_data(df: pd.DataFrame) -> Dict[str, float]:
    """テクニカル指標からPivot PointsのS/Rを取得する"""
    pivot_data = {}
    if df.empty:
        return pivot_data
    
    # 最新の行を取得
    last_row = df.iloc[-1]
    
    # Standard Pivot Pointsの値を取得
    pivot_data['pp'] = last_row.get('PP_D', np.nan)
    pivot_data['r1'] = last_row.get('R1_D', np.nan)
    pivot_data['r2'] = last_row.get('R2_D', np.nan)
    pivot_data['r3'] = last_row.get('R3_D', np.nan)
    pivot_data['s1'] = last_row.get('S1_D', np.nan)
    pivot_data['s2'] = last_row.get('S2_D', np.nan)
    pivot_data['s3'] = last_row.get('S3_D', np.nan)
    
    # NaNを0.0に変換して返す (NoneTypeエラー回避のため)
    return {k: v if not np.isnan(v) else 0.0 for k, v in pivot_data.items()}

def calculate_score_long(last_row: pd.Series, prev_row: pd.Series, timeframe: str) -> float:
    """ロングシグナルのスコアを計算する"""
    score = BASE_SCORE # 0.40点からスタート
    tech_data = {}

    # 1. 価格とボリューム
    close = last_row.get('Close', np.nan)
    volume = last_row.get('Volume', np.nan)
    open_val = last_row.get('Open', np.nan)
    prev_close = prev_row.get('Close', np.nan)
    
    # 2. テクニカル指標の値を取得 (.get()でKeyError回避済み)
    rsi = last_row.get('RSI_14', np.nan)
    adx = last_row.get('ADX_14', np.nan)
    pdi = last_row.get('DMP_14', np.nan)
    ndi = last_row.get('DMN_14', np.nan)
    macd_hist = last_row.get('MACDh_12_26_9', np.nan)
    cci = last_row.get('CCI_20', np.nan)
    stoch_k = last_row.get('STOCHRSIk_14_14_3_3', np.nan)
    bbp = last_row.get('BBP_20_2.0', np.nan)
    bbw = last_row.get('BBW', np.nan)
    bb_lower = last_row.get('BBL_20_2.0', np.nan)
    long_term_sma = last_row.get(f'SMA_{LONG_TERM_SMA_LENGTH}', np.nan)
    regime = last_row.get('REGIME', np.nan)
    vwap_proxy = last_row.get('VWAP_Proxy', np.nan)

    # ----------------------------------------------------
    # A. トレンド・方向性の確証
    # ----------------------------------------------------
    
    # A1. MACDヒストグラム
    if macd_hist > 0: score += 0.05
    elif macd_hist < 0: score -= 0.05
    
    # A2. ADXとDI
    if adx > ADX_TREND_THRESHOLD:
        if pdi > ndi: score += 0.05
        else: score -= 0.05
    
    # ----------------------------------------------------
    # B. モメンタムと過熱感
    # ----------------------------------------------------

    # B1. RSIモメンタム
    if rsi < RSI_MOMENTUM_LOW:
        if rsi > prev_row.get('RSI_14', np.nan): score += 0.05
        else: score -= 0.05
    elif rsi >= RSI_MOMENTUM_LOW and rsi <= RSI_MOMENTUM_HIGH:
        score += 0.05
        
    # B2. RSI過熱感
    if rsi <= RSI_OVERSOLD: score += 0.05
    elif rsi >= RSI_OVERBOUGHT: score -= 0.05

    # B3. CCI
    if cci < -100: score += 0.03
    elif cci > 100: score -= 0.03
        
    # B4. StochRSIフィルター
    stoch_penalty_value = 0.0
    if stoch_k > 80: 
        stoch_penalty_value = 0.10
        score -= stoch_penalty_value
    
    # ----------------------------------------------------
    # C. ボラティリティとバンド
    # ----------------------------------------------------
    
    # C1. BBP
    if bbp <= 0.2: score += 0.05
    
    # C2. バンド幅
    if bbw is not np.nan and bbw < VOLATILITY_BB_PENALTY_THRESHOLD:
        score += 0.05
    elif close < bb_lower:
        score += 0.05

    # ----------------------------------------------------
    # D. 長期トレンドとMACDクロスによるペナルティ
    # ----------------------------------------------------
    long_term_trend = 'Neutral'
    long_term_reversal_penalty = False
    long_term_reversal_penalty_value = 0.0
    if not np.isnan(long_term_sma):
        if close < long_term_sma:
            long_term_trend = 'Down'
            if timeframe in ['15m', '1h']: 
                score -= LONG_TERM_REVERSAL_PENALTY 
                long_term_reversal_penalty = True
                long_term_reversal_penalty_value = LONG_TERM_REVERSAL_PENALTY
        elif close > long_term_sma:
            long_term_trend = 'Up'
        
    # D2. MACDクロスの信頼性フィルター
    macd_valid = True
    macd_penalty_value = 0.0
    macd_line = last_row.get('MACD_12_26_9', np.nan)
    macd_signal = last_row.get('MACDs_12_26_9', np.nan)
    
    if macd_line < macd_signal:
        score -= MACD_CROSS_PENALTY
        macd_valid = False
        macd_penalty_value = MACD_CROSS_PENALTY

    # D3. VWAPとの位置関係
    vwap_consistent = close > vwap_proxy
    if not vwap_consistent: score -= 0.05

    # ----------------------------------------------------
    # E. 構造的S/Rボーナス/ペナルティ
    # ----------------------------------------------------
    pivot_points = get_pivot_points_data(last_row.to_frame().T)
    s1 = pivot_points.get('s1', 0.0)
    s2 = pivot_points.get('s2', 0.0)
    
    structural_pivot_bonus = 0.0
    
    # E1. 価格が強力なサポートS1/S2付近にある場合、ボーナス
    if s1 > 0 and close > s1 and close < s1 * 1.003: 
        structural_pivot_bonus = 0.07 
    elif s2 > 0 and close > s2 and close < s2 * 1.003: 
        structural_pivot_bonus = 0.10
        
    score += structural_pivot_bonus

    # E2. 価格がエントリーすべきでない抵抗線 R1/R2 の付近にある場合、ペナルティ
    r1 = pivot_points.get('r1', 0.0)
    r2 = pivot_points.get('r2', 0.0)
    
    if r1 > 0 and close < r1 and close > r1 * 0.997: 
         structural_pivot_bonus = -0.05 
    elif r2 > 0 and close < r2 and close > r2 * 0.997: 
        structural_pivot_bonus = -0.10 
        
    score += structural_pivot_bonus 

    # ----------------------------------------------------
    # F. 出来高の確証
    # ----------------------------------------------------
    volume_confirmation_bonus = 0.0
    
    # volume_smaはここでは計算されないため、簡易的な前足との比較を用いる
    if volume is not np.nan and prev_row.get('Volume', np.nan) is not np.nan:
        if close > open_val and volume > prev_row.get('Volume', np.nan) * VOLUME_CONFIRMATION_MULTIPLIER:
            volume_confirmation_bonus = 0.10
            
    score += volume_confirmation_bonus
    
    # ----------------------------------------------------
    # H. 最終的なスコアのクリップ
    # ----------------------------------------------------
    score = max(0.0, min(1.0, score))

    # テクニカルデータ辞書の構築
    tech_data = {
        'rsi': rsi, 'adx': adx, 'pdi': pdi, 'ndi': ndi, 
        'macd_hist': macd_hist, 'cci': cci, 'stoch_k': stoch_k, 
        'bbp': bbp, 'bbw': bbw, 
        'long_term_sma': long_term_sma, 'long_term_trend': long_term_trend,
        'regime': regime, 'vwap_proxy': vwap_proxy,
        
        'long_term_reversal_penalty': long_term_reversal_penalty,
        'long_term_reversal_penalty_value': long_term_reversal_penalty_value,
        'macd_cross_valid': macd_valid,
        'macd_cross_penalty_value': macd_penalty_value,
        'stoch_filter_penalty': stoch_penalty_value,
        'vwap_consistent': vwap_consistent,
        'structural_pivot_bonus': structural_pivot_bonus,
        'volume_confirmation_bonus': volume_confirmation_bonus,
        
        'atr_value': last_row.get('ATR_14', np.nan),
        'pivot_points': pivot_points
    }
    
    return score, tech_data

def calculate_score_short(last_row: pd.Series, prev_row: pd.Series, timeframe: str) -> float:
    """ショートシグナルのスコアを計算する (ロングシグナルのロジックを反転)"""
    score = BASE_SCORE # 0.40点からスタート
    tech_data = {}

    # 1. 価格とボリューム
    close = last_row.get('Close', np.nan)
    volume = last_row.get('Volume', np.nan)
    open_val = last_row.get('Open', np.nan)
    prev_close = prev_row.get('Close', np.nan)
    
    # 2. テクニカル指標の値を取得 (.get()でKeyError回避済み)
    rsi = last_row.get('RSI_14', np.nan)
    adx = last_row.get('ADX_14', np.nan)
    pdi = last_row.get('DMP_14', np.nan)
    ndi = last_row.get('DMN_14', np.nan)
    macd_hist = last_row.get('MACDh_12_26_9', np.nan)
    cci = last_row.get('CCI_20', np.nan)
    stoch_k = last_row.get('STOCHRSIk_14_14_3_3', np.nan)
    bbp = last_row.get('BBP_20_2.0', np.nan)
    bbw = last_row.get('BBW', np.nan)
    bb_upper = last_row.get('BBU_20_2.0', np.nan)
    long_term_sma = last_row.get(f'SMA_{LONG_TERM_SMA_LENGTH}', np.nan)
    regime = last_row.get('REGIME', np.nan)
    vwap_proxy = last_row.get('VWAP_Proxy', np.nan)

    # ----------------------------------------------------
    # A. トレンド・方向性の確証
    # ----------------------------------------------------
    
    # A1. MACDヒストグラム
    if macd_hist < 0: score += 0.05
    elif macd_hist > 0: score -= 0.05
    
    # A2. ADXとDI
    if adx > ADX_TREND_THRESHOLD:
        if ndi > pdi: score += 0.05
        else: score -= 0.05
    
    # ----------------------------------------------------
    # B. モメンタムと過熱感
    # ----------------------------------------------------

    # B1. RSIモメンタム
    if rsi > RSI_MOMENTUM_HIGH:
        if rsi < prev_row.get('RSI_14', np.nan): score += 0.05
        else: score -= 0.05
    elif rsi <= RSI_MOMENTUM_HIGH and rsi >= RSI_MOMENTUM_LOW:
        score += 0.05
        
    # B2. RSI過熱感
    if rsi >= RSI_OVERBOUGHT: score += 0.05
    elif rsi <= RSI_OVERSOLD: score -= 0.05

    # B3. CCI
    if cci > 100: score += 0.03
    elif cci < -100: score -= 0.03
        
    # B4. StochRSIフィルター
    stoch_penalty_value = 0.0
    if stoch_k < 20: 
        stoch_penalty_value = 0.10
        score -= stoch_penalty_value

    # ----------------------------------------------------
    # C. ボラティリティとバンド
    # ----------------------------------------------------
    
    # C1. BBP
    if bbp >= 0.8: score += 0.05
    
    # C2. バンド幅
    if bbw is not np.nan and bbw < VOLATILITY_BB_PENALTY_THRESHOLD:
        score += 0.05
    elif close > bb_upper:
        score += 0.05

    # ----------------------------------------------------
    # D. 長期トレンドとMACDクロスによるペナルティ
    # ----------------------------------------------------
    long_term_trend = 'Neutral'
    long_term_reversal_penalty = False
    long_term_reversal_penalty_value = 0.0
    if not np.isnan(long_term_sma):
        if close > long_term_sma:
            long_term_trend = 'Up'
            if timeframe in ['15m', '1h']: 
                score -= LONG_TERM_REVERSAL_PENALTY 
                long_term_reversal_penalty = True
                long_term_reversal_penalty_value = LONG_TERM_REVERSAL_PENALTY
        elif close < long_term_sma:
            long_term_trend = 'Down'

    # D2. MACDクロスの信頼性フィルター
    macd_valid = True
    macd_penalty_value = 0.0
    macd_line = last_row.get('MACD_12_26_9', np.nan)
    macd_signal = last_row.get('MACDs_12_26_9', np.nan)
    
    if macd_line > macd_signal:
        score -= MACD_CROSS_PENALTY
        macd_valid = False
        macd_penalty_value = MACD_CROSS_PENALTY
        
    # D3. VWAPとの位置関係
    vwap_consistent = close < vwap_proxy
    if not vwap_consistent: score -= 0.05

    # ----------------------------------------------------
    # E. 構造的S/Rボーナス/ペナルティ
    # ----------------------------------------------------
    pivot_points = get_pivot_points_data(last_row.to_frame().T)
    r1 = pivot_points.get('r1', 0.0)
    r2 = pivot_points.get('r2', 0.0)
    
    structural_pivot_bonus = 0.0
    
    # E1. 価格が強力なレジスタンスR1/R2付近にある場合、ボーナス
    if r1 > 0 and close < r1 and close > r1 * 0.997: 
        structural_pivot_bonus = 0.07
    elif r2 > 0 and close < r2 and close > r2 * 0.997: 
        structural_pivot_bonus = 0.10
        
    score += structural_pivot_bonus

    # E2. 価格がエントリーすべきでないサポート S1/S2 の付近にある場合、ペナルティ
    s1 = pivot_points.get('s1', 0.0)
    s2 = pivot_points.get('s2', 0.0)
    
    if s1 > 0 and close > s1 and close < s1 * 1.003: 
         structural_pivot_bonus = -0.05
    elif s2 > 0 and close > s2 and close < s2 * 1.003: 
        structural_pivot_bonus = -0.10
        
    score += structural_pivot_bonus 

    # ----------------------------------------------------
    # F. 出来高の確証
    # ----------------------------------------------------
    volume_confirmation_bonus = 0.0
    
    # volume_smaはここでは計算されないため、簡易的な前足との比較を用いる
    if volume is not np.nan and prev_row.get('Volume', np.nan) is not np.nan:
        if close < open_val and volume > prev_row.get('Volume', np.nan) * VOLUME_CONFIRMATION_MULTIPLIER:
            volume_confirmation_bonus = 0.10
            
    score += volume_confirmation_bonus
    
    # ----------------------------------------------------
    # H. 最終的なスコアのクリップ
    # ----------------------------------------------------
    score = max(0.0, min(1.0, score))
    
    # テクニカルデータ辞書の構築
    tech_data = {
        'rsi': rsi, 'adx': adx, 'pdi': pdi, 'ndi': ndi, 
        'macd_hist': macd_hist, 'cci': cci, 'stoch_k': stoch_k, 
        'bbp': bbp, 'bbw': bbw, 
        'long_term_sma': long_term_sma, 'long_term_trend': long_term_trend,
        'regime': regime, 'vwap_proxy': vwap_proxy,

        'long_term_reversal_penalty': long_term_reversal_penalty,
        'long_term_reversal_penalty_value': long_term_reversal_penalty_value,
        'macd_cross_valid': macd_valid,
        'macd_cross_penalty_value': macd_penalty_value,
        'stoch_filter_penalty': stoch_penalty_value,
        'vwap_consistent': vwap_consistent,
        'structural_pivot_bonus': structural_pivot_bonus,
        'volume_confirmation_bonus': volume_confirmation_bonus,

        'atr_value': last_row.get('ATR_14', np.nan),
        'pivot_points': pivot_points
    }
    
    return score, tech_data

def calculate_rr_ratio_and_stops(last_row: pd.Series, side: str, tech_data: Dict) -> Tuple[float, float, float, str, bool]:
    """
    動的追跡ストップ (DTS) に基づくRRR、SL、TPを計算する。
    """
    close = last_row.get('Close', np.nan)
    atr = tech_data.get('atr_value', np.nan)
    pivot_points = tech_data.get('pivot_points', {})
    
    # データがない場合は処理をスキップ
    if np.isnan(close) or np.isnan(atr) or atr <= 0:
        return 0.0, 0.0, 0.0, 'N/A', False

    # 1. リスク許容度 (ATRに基づく SL幅)
    risk_width = atr * ATR_TRAIL_MULTIPLIER
    
    # 2. 利益目標 (RRRに基づく TP幅)
    reward_width_display = risk_width * DTS_RRR_DISPLAY
    
    # 3. エントリー戦略と SL/TP の計算
    entry_type = 'Market'
    sl_price = 0.0
    tp_price_display = 0.0
    structural_sl_used = False
    
    # ロング
    if side == "ロング":
        entry_price = close - atr 
        s1 = pivot_points.get('s1', 0.0)
        s2 = pivot_points.get('s2', 0.0)
        
        if s1 > 0 and s1 < entry_price and (entry_price - s1) < risk_width * 1.5:
            sl_candidate = s1
            sl_price = sl_candidate - (0.5 * atr) 
            structural_sl_used = True
            risk_width = entry_price - sl_price
            entry_type = 'Limit (S1/ATR)'
        else:
            sl_price = close - risk_width
            entry_type = 'Limit (ATR)'
        
        r2 = pivot_points.get('r2', 0.0)
        if r2 > 0 and r2 > close:
            tp_price_display = r2
        else:
            tp_price_display = entry_price + reward_width_display

    # ショート
    elif side == "ショート":
        entry_price = close + atr
        r1 = pivot_points.get('r1', 0.0)
        r2 = pivot_points.get('r2', 0.0)

        if r1 > 0 and r1 > entry_price and (r1 - entry_price) < risk_width * 1.5:
            sl_candidate = r1
            sl_price = sl_candidate + (0.5 * atr)
            structural_sl_used = True
            risk_width = sl_price - entry_price
            entry_type = 'Limit (R1/ATR)'
        else:
            sl_price = close + risk_width
            entry_type = 'Limit (ATR)'

        s2 = pivot_points.get('s2', 0.0)
        if s2 > 0 and s2 < close:
            tp_price_display = s2
        else:
            tp_price_display = entry_price - reward_width_display
    
    if risk_width <= 0:
        return 0.0, 0.0, 0.0, 'N/A', structural_sl_used
        
    reward_width_calculated = abs(tp_price_display - entry_price)
    rr_ratio_calculated = reward_width_calculated / risk_width
        
    return rr_ratio_calculated, entry_price, sl_price, entry_type, structural_sl_used

def analyze_single_timeframe(symbol: str, timeframe: str) -> Dict:
    """単一の時間軸でテクニカル分析を行い、シグナルを生成する"""
    
    # 1. OHLCVデータを取得
    limit = REQUIRED_OHLCV_LIMITS.get(timeframe, 500)
    df = asyncio.run(fetch_ohlcv_data(symbol, timeframe, limit))
    
    # データ不足の場合の処理 (IndexErrorの最初のチェックポイント)
    # iloc[-2] のアクセスが必要なため、最低2行必要
    if df is None or df.empty or len(df) < 2:
        logging.warning(f"分析失敗: {symbol} {timeframe} のデータが {len(df) if df is not None else 0} 行で不十分です（最低2行必要）。")
        return {
            'symbol': symbol, 'timeframe': timeframe, 'side': 'DataShortage', 
            'score': 0.0, 'rr_ratio': 0.0, 'price': 0.0, 
            'tech_data': {'error': 'DataShortage'}
        }
        
    # 2. テクニカル指標を計算
    df = calculate_technical_indicators(df, timeframe)
    
    # V17.0.5 FIX: IndexErrorの可能性を完全に排除するため、テクニカル計算後に再度チェック
    if df.empty or len(df) < 2:
        logging.error(f"分析失敗: {symbol} {timeframe} のデータ処理後にデータフレームが不正です (サイズ < 2)。")
        return {
            'symbol': symbol, 'timeframe': timeframe, 'side': 'DataShortage', 
            'score': 0.0, 'rr_ratio': 0.0, 'price': 0.0, 
            'tech_data': {'error': 'PostProcessingDataIssue'}
        }

    # 3. 最新のデータ行を取得 (IndexErrorが発生しないことが保証される)
    last_row = df.iloc[-1]
    prev_row = df.iloc[-2]

    # データがNaNなどで不完全な場合はエラーとする
    if last_row.isnull().all():
        logging.error(f"分析失敗: {symbol} {timeframe} の最新行データが不正です (NaN多数)。")
        return {
            'symbol': symbol, 'timeframe': timeframe, 'side': 'DataShortage', 
            'score': 0.0, 'rr_ratio': 0.0, 'price': 0.0, 
            'tech_data': {'error': 'InvalidData'}
        }

    current_price = last_row.get('Close', np.nan)
    
    # 4. スコア計算
    score_long, tech_data_long = calculate_score_long(last_row, prev_row, timeframe)
    score_short, tech_data_short = calculate_score_short(last_row, prev_row, timeframe)
    
    # 5. RRRとSL/TPの計算
    if score_long >= score_short and score_long >= BASE_SCORE:
        side = "ロング"
        score = score_long
        tech_data = tech_data_long
    elif score_short > score_long and score_short >= BASE_SCORE:
        side = "ショート"
        score = score_short
        tech_data = tech_data_short
    else:
        side = "Neutral"
        score = max(score_long, score_short)
        tech_data = tech_data_long if score_long > score_short else tech_data_short
        
    # 6. RRRとSL/TPの計算 (シグナルがある場合のみ)
    if side != 'Neutral':
        rr_ratio, entry_price, sl_price, entry_type, structural_sl_used = calculate_rr_ratio_and_stops(last_row, side, tech_data)
        
        tech_data['structural_sl_used'] = structural_sl_used
        
        # 7. 最終的なリターン
        return {
            'symbol': symbol,
            'timeframe': timeframe,
            'side': side,
            'score': score,
            'rr_ratio': rr_ratio,
            'price': current_price,
            'entry': entry_price,
            'sl': sl_price,
            'tp1': entry_price + (entry_price - sl_price) * rr_ratio if side == "ロング" else entry_price - (sl_price - entry_price) * rr_ratio,
            'entry_type': entry_type,
            'tech_data': tech_data
        }
    else:
        # Neutralシグナルのリターン (最低限の情報)
        return {
            'symbol': symbol,
            'timeframe': timeframe,
            'side': 'Neutral',
            'score': score,
            'rr_ratio': 0.0,
            'price': current_price,
            'tech_data': tech_data
        }

# ====================================================================================
# CORE BOT LOGIC
# ====================================================================================

async def get_macro_context() -> Dict:
    """
    BTCのトレンド、ドミナンス、FGIのプロキシなど、市場全体のマクロな状況を取得する
    """
    global EXCHANGE_CLIENT
    
    context: Dict[str, Any] = {
        'btc_trend_4h': 'N/A',
        'btc_change_24h': 0.0,
        'sentiment_fgi_proxy': 0.0,
        'dominance_trend': 'N/A',
        'dominance_bias_value': 0.0,
        'funding_rate_bias': 0.0,
        'funding_rate_penalty_value': 0.0,
    }

    # --- 1. BTCの4hトレンド判定 (SMA50使用) ---
    btc_df_4h = await fetch_ohlcv_data("BTC-USDT", "4h", 60) # 60本でSMA50を計算
    
    if btc_df_4h is not None and not btc_df_4h.empty:
        btc_df_4h = calculate_technical_indicators(btc_df_4h, '4h')
        
        # IndexError防止のため、最新の行が存在するかチェック
        if not btc_df_4h.empty:
            last_row = btc_df_4h.iloc[-1]
            
            btc_close = last_row.get('Close', np.nan)
            btc_sma50 = last_row.get(f'SMA_{LONG_TERM_SMA_LENGTH}', np.nan)

            if not np.isnan(btc_close) and not np.isnan(btc_sma50):
                if btc_close > btc_sma50:
                    context['btc_trend_4h'] = 'Up'
                elif btc_close < btc_sma50:
                    context['btc_trend_4h'] = 'Down'
    
    # --- 2. BTCの24h変動率 ---
    # 24時間前のCloseと現在のCloseを比較 (4h * 6 = 24h -> 7本前のデータにアクセス)
    if btc_df_4h is not None and len(btc_df_4h) >= 7: # Index Error防止
        current_close = btc_df_4h.iloc[-1].get('Close', np.nan)
        prev_24h_close = btc_df_4h.iloc[-7].get('Close', np.nan) 
        
        if not np.isnan(current_close) and not np.isnan(prev_24h_close) and prev_24h_close > 0:
            change = ((current_close - prev_24h_close) / prev_24h_close) * 100
            context['btc_change_24h'] = change

    # --- 3. BTCドミナンスのトレンド判定 ---
    try:
        btc_dominance = yf.download("BTC-USD.D", period="5d", interval="60m")
        if not btc_dominance.empty and len(btc_dominance) > 5:
            btc_dominance['SMA_5'] = btc_dominance['Close'].rolling(window=5).mean()
            # iloc[-1]アクセス前にチェック
            if not btc_dominance['Close'].empty and not btc_dominance['SMA_5'].empty:
                last_close = btc_dominance['Close'].iloc[-1]
                last_sma = btc_dominance['SMA_5'].iloc[-1]
                
                if last_close > last_sma:
                    context['dominance_trend'] = 'Up'
                    context['dominance_bias_value'] = -DOMINANCE_BIAS_BONUS_PENALTY
                elif last_close < last_sma:
                    context['dominance_trend'] = 'Down'
                    context['dominance_bias_value'] = +DOMINANCE_BIAS_BONUS_PENALTY
                else:
                    context['dominance_trend'] = 'Neutral'
            
    except Exception as e:
        logging.warning(f"BTC Dominanceデータ取得失敗: {e}")

    # --- 4. 簡易FGI (恐怖&貪欲指数) プロキシ ---
    if btc_df_4h is not None and not btc_df_4h.empty:
        last_row = btc_df_4h.iloc[-1]
        atr_pct = (last_row.get('ATR_14', 0.0) / last_row.get('Close', 1.0)) * 100 
        
        fgi_proxy = (context['btc_change_24h'] * 0.5) - (atr_pct * 0.5)
        
        context['sentiment_fgi_proxy'] = max(-1.0, min(1.0, fgi_proxy / 10.0))

    # --- 5. OKX Funding Rate Bias ---
    if EXCHANGE_CLIENT:
        try:
            if hasattr(EXCHANGE_CLIENT, 'publicGetPublicV2FundingRate'):
                rate_data = await EXCHANGE_CLIENT.publicGetPublicV2FundingRate({'instId': 'BTC-USDT-SWAP'})
                # OKXのレスポンス形式に依存するが、ここではデータが存在することを前提
                if rate_data and 'data' in rate_data and rate_data['data']:
                    rate = float(rate_data['data'][0]['fundingRate'])
                    
                    context['funding_rate_bias'] = rate

                    if rate > FUNDING_RATE_THRESHOLD: 
                        context['funding_rate_penalty_value'] = -FUNDING_RATE_BONUS_PENALTY
                    elif rate < -FUNDING_RATE_THRESHOLD: 
                        context['funding_rate_penalty_value'] = +FUNDING_RATE_BONUS_PENALTY

        except Exception as e:
            logging.warning(f"Funding Rateデータ取得失敗: {e}")

    logging.info(f"🌍 マクロコンテキスト更新: BTC Trend({context['btc_trend_4h']}), BTC Change({context['btc_change_24h']:.2f}%), FGI Proxy({context['sentiment_fgi_proxy']:.2f}%), Dominance Trend({context['dominance_trend']})")
    
    return context

async def get_top_volume_symbols() -> List[str]:
    """
    取引所から出来高の高いシンボルを取得し、既存のリストと統合する
    (時間とリソース節約のため、ここではDEFAULT_SYMBOLSを返す)
    """
    global EXCHANGE_CLIENT
    
    if EXCHANGE_CLIENT is None:
        return [s.replace('/', '-') for s in DEFAULT_SYMBOLS[:TOP_SYMBOL_LIMIT]] 

    try:
        # 動的取得の実装は省略し、デフォルトリストを使用
        top_symbols = [s.replace('/', '-') for s in DEFAULT_SYMBOLS[:TOP_SYMBOL_LIMIT]]
        
        logging.info(f"✅ 出来高トップ {TOP_SYMBOL_LIMIT} 銘柄を更新しました (デフォルト)。")
        
        new_symbols = set(top_symbols) - set(CURRENT_MONITOR_SYMBOLS)
        if new_symbols:
             logging.info(f"🆕 新規追加銘柄 (Default外): {', '.join(new_symbols)}")

        return top_symbols
        
    except Exception as e:
        logging.error(f"出来高トップシンボルの取得に失敗しました: {e}。デフォルトリストを使用します。")
        return [s.replace('/', '-') for s in DEFAULT_SYMBOLS[:TOP_SYMBOL_LIMIT]] 

async def main_loop():
    """
    ボットのメイン実行ループ
    """
    global LAST_UPDATE_TIME, CURRENT_MONITOR_SYMBOLS, LAST_SUCCESS_TIME, GLOBAL_MACRO_CONTEXT, LAST_ANALYSIS_SIGNALS, LAST_SUCCESSFUL_MONITOR_SYMBOLS
    
    # 1. CCXTクライアントの初期化
    await initialize_ccxt_client(CCXT_CLIENT_NAME)

    while True:
        try:
            # 冷却期間のチェック (LOOP_INTERVAL秒ごとに実行)
            current_time = time.time()
            if current_time - LAST_UPDATE_TIME < LOOP_INTERVAL:
                await asyncio.sleep(LOOP_INTERVAL - (current_time - LAST_UPDATE_TIME))
                continue
            
            LAST_UPDATE_TIME = current_time

            # 2. マクロコンテキストの取得
            GLOBAL_MACRO_CONTEXT = await get_macro_context()
            
            # 3. 監視銘柄リストの動的更新
            CURRENT_MONITOR_SYMBOLS = await get_top_volume_symbols()
            symbols_to_monitor = CURRENT_MONITOR_SYMBOLS.copy()
            logging.info(f"💡 分析開始。監視銘柄数: {len(symbols_to_monitor)}")
            
            all_signals: List[Dict] = []
            
            # 4. 各銘柄の分析を並行して実行 (遅延を考慮)
            for symbol in symbols_to_monitor:
                
                # Cooldownチェック
                last_notify_time = TRADE_NOTIFIED_SYMBOLS.get(symbol, 0.0)
                if current_time - last_notify_time < TRADE_SIGNAL_COOLDOWN:
                    logging.debug(f"スキップ: {symbol} はクールダウン中です。")
                    continue
                    
                # 3つの時間軸で分析
                tasks = [
                    analyze_single_timeframe(symbol, '15m'),
                    analyze_single_timeframe(symbol, '1h'),
                    analyze_single_timeframe(symbol, '4h'),
                ]
                
                # 非同期で実行
                results = await asyncio.gather(*tasks)
                
                # 結果を統合
                integrated_signals = [r for r in results if r.get('side') != 'DataShortage']
                
                # マクロコンテキストをシグナルに反映
                for signal in integrated_signals:
                    score_adjustment = 0.0
                    
                    # Funding Rate Bias
                    funding_penalty = GLOBAL_MACRO_CONTEXT.get('funding_rate_penalty_value', 0.0)
                    if funding_penalty != 0.0:
                        if signal.get('side') == 'ロング' and funding_penalty < 0:
                            score_adjustment += funding_penalty
                        elif signal.get('side') == 'ショート' and funding_penalty > 0:
                            score_adjustment += -funding_penalty
                            
                    # Dominance Bias (Altcoinのみ)
                    if 'BTC' not in symbol:
                        dominance_bias = GLOBAL_MACRO_CONTEXT.get('dominance_bias_value', 0.0)
                        score_adjustment += dominance_bias
                        signal['macro_context']['dominance_bias_value'] = dominance_bias

                    # 最終スコアに調整値を反映
                    original_score = signal.get('score', BASE_SCORE)
                    new_score = max(0.0, min(1.0, original_score + score_adjustment))
                    
                    signal['score'] = new_score
                    signal['macro_context'] = GLOBAL_MACRO_CONTEXT
                    
                    all_signals.append(signal)

                # API遅延
                await asyncio.sleep(REQUEST_DELAY_PER_SYMBOL)

            # 5. シグナルランキングと通知
            high_confidence_signals = [s for s in all_signals if s.get('score', 0.0) >= SIGNAL_THRESHOLD and s.get('side') != 'Neutral']
            
            # ソート: スコア > RRR > ADX > -ATR の順
            final_ranking = sorted(
                high_confidence_signals, 
                key=lambda s: (
                    s.get('score', 0.0), 
                    s.get('rr_ratio', 0.0), 
                    s.get('tech_data', {}).get('adx', 0.0), 
                    -s.get('tech_data', {}).get('atr_value', 1.0),
                    s.get('symbol', '')
                ), 
                reverse=True
            )
            
            LAST_ANALYSIS_SIGNALS = final_ranking[:TOP_SIGNAL_COUNT]
            
            # TOP N シグナルの通知
            rank = 1
            for signal_dict in final_ranking[:TOP_SIGNAL_COUNT]:
                symbol = signal_dict.get('symbol')
                
                last_notify_time = TRADE_NOTIFIED_SYMBOLS.get(symbol, 0.0)
                if current_time - last_notify_time < TRADE_SIGNAL_COOLDOWN:
                    logging.info(f"通知スキップ: {symbol} はクールダウン中です。")
                    continue
                    
                symbol_signals = [s for s in all_signals if s.get('symbol') == symbol]
                
                message = format_integrated_analysis_message(symbol, symbol_signals, rank)
                
                if message:
                    send_telegram_html(message)
                    TRADE_NOTIFIED_SYMBOLS[symbol] = current_time
                    rank += 1
            
            LAST_SUCCESS_TIME = time.time()
            LAST_SUCCESSFUL_MONITOR_SYMBOLS = symbols_to_monitor.copy()
            
            logging.info("♻️ 成功: メインループを完了しました。次の実行まで待機します。")
            
            await asyncio.sleep(LOOP_INTERVAL)

        except Exception as e:
            error_name = type(e).__name__
            logging.error(f"メインループで致命的なエラー: {error_name} - {e}")
            
            # 例外が発生してもボットが停止しないように、一時停止して再試行
            await asyncio.sleep(60)


# ====================================================================================
# FASTAPI SETUP
# ====================================================================================

app = FastAPI(title="Apex BOT API", version="v17.0.5 - IndexError Robustness Fix") # バージョン更新

@app.on_event("startup")
async def startup_event():
    """アプリケーション起動時にメインループタスクを開始"""
    logging.info("🚀 Apex BOT v17.0.5 Startup initializing...") # バージョン更新
    asyncio.create_task(main_loop())

@app.on_event("shutdown")
async def shutdown_event():
    """アプリケーション終了時にCCXTクライアントをクローズ"""
    global EXCHANGE_CLIENT
    if EXCHANGE_CLIENT:
        await EXCHANGE_CLIENT.close()
        logging.info("CCXTクライアントをシャットダウンしました。")

@app.get("/status")
def get_status():
    """ボットのステータスを返すエンドポイント"""
    status_msg = {
        "status": "ok",
        "bot_version": "v17.0.5 - IndexError Robustness Fix", # バージョン更新
        "last_success_time_utc": datetime.fromtimestamp(LAST_SUCCESS_TIME, tz=timezone.utc).isoformat() if LAST_SUCCESS_TIME else "N/A",
        "current_client": CCXT_CLIENT_NAME,
        "monitoring_symbols": len(CURRENT_MONITOR_SYMBOLS),
        "last_signals_count": len(LAST_ANALYSIS_SIGNALS)
    }
    return JSONResponse(content=status_msg)

@app.head("/")
@app.get("/")
def home_view():
    """Renderのヘルスチェック用エンドポイント"""
    return JSONResponse(content={"message": "Apex BOT is running (v17.0.5)"})
