# ====================================================================================
# Apex BOT v17.0.6 - Fix Persistent CCXT AttributeError
# - FIX: initialize_ccxt_client 関数内で、既存クライアントを閉じた後に明示的に EXCHANGE_CLIENT = None を設定し、グローバル参照を確実にクリア。
# - FIX: main_loop のエラー回復ロジック内で、CCXTクライアント再初期化後に asyncio.sleep(3) を追加し、新しい aiohttp セッションが安定する時間を確保。
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
POSITION_CAPITAL = 1000.0           # 1x想定のポジションサイズ (USD) - NEW

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
    3つの時間軸の分析結果を統合し、ログメッセージの形式に整形する (v17.0.6対応)
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
        f"| ❌ SL実行時 | **{format_pnl_utility_telegram(-sl_loss_usd)}** | {sl_risk_percent:.2f}% |\n" 
        f"| 🟢 TP目標時 | **{format_pnl_utility_telegram(tp_gain_usd)}** | {tp_gain_percent:.2f}% |\n"
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
                # FIX: regimeをtech_dataから取得
                regime = best_signal.get('tech_data', {}).get('regime', 'N/A')
                
                # ADX/Regime
                analysis_detail += f"   └ **ADX/Regime**: {tech_data.get('adx', 0.0):.2f} ({regime})\n"
                # RSI/MACDH/CCI/STOCH
                analysis_detail += f"   └ **RSI/MACDH/CCI**: {tech_data.get('rsi', 0.0):.2f} / {tech_data.get('macd_hist', 0.0):.4f} / {tech_data.get('cci', 0.0):.2f}\n"

                # NEW: 構造的S/Rボーナスのハイライト
                pivot_bonus = tech_data.get('structural_pivot_bonus', 0.0)
                pivot_status = "❌ 構造確証なし"
                if pivot_bonus > 0:
                     pivot_status = f"✅ **構造的S/R確証**"
                analysis_detail += f"   └ **構造分析(Pivot)**: {pivot_status} (<ins>**+{pivot_bonus * 100:.2f}点 ボーナス追加**</ins>)\n"

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
                dominance_bias_bonus = tech_data.get('dominance_bias_bonus_value', 0.0)
                
                dominance_status = ""
                if dominance_bias_bonus > 0:
                    dominance_status = f"✅ **優位性あり** (<ins>**+{dominance_bias_bonus * 100:.2f}点**</ins>)"
                elif dominance_bias_bonus < 0:
                    dominance_status = f"⚠️ **バイアスにより減点適用** (<ins>**-{abs(dominance_bias_bonus) * 100:.2f}点**</ins>)"
                else:
                    dominance_status = "❌ フィルター範囲外/非該当"
                
                trend_display = ""
                if symbol != 'BTC-USDT':
                     trend_display = f" (Altcoin Bias: {dominance_trend})"
                
                analysis_detail += f"   └ **BTCドミナンス**: {dominance_trend} トレンド{trend_display} - {dominance_status}\n"


    # 3. リスク管理とフッター
    # FIX: regimeをtech_dataから取得
    regime = best_signal.get('tech_data', {}).get('regime', 'N/A')
    
    footer = (
        f"==================================\n"
        f"| 🔍 **市場環境** | **{regime}** 相場 (ADX: {best_signal.get('tech_data', {}).get('adx', 0.0):.2f}) |\n"
        f"| ⚙️ **BOT Ver** | **v17.0.6** - CCXT Stability Fix |\n" # バージョン更新
        f"==================================\n"
        f"\n<pre>※ Limit注文は、価格が指定水準に到達した際のみ約定します。DTS戦略では、価格が有利な方向に動いた場合、SLが自動的に追跡され利益を最大化します。</pre>"
    )

    return header + trade_plan + pnl_block + pivot_pnl_block + sr_info + analysis_detail + footer


# ====================================================================================
# CCXT & DATA ACQUISITION
# ====================================================================================

async def initialize_ccxt_client():
    """CCXTクライアントを初期化 (OKX) - Stability Fix (v17.0.6)適用"""
    global EXCHANGE_CLIENT
    
    # NEW FIX 1: 既存のクライアントがあれば、安全に閉じ、明示的にNoneに設定する
    if EXCHANGE_CLIENT:
        try:
            # CCXTクライアントの非同期セッションを閉じる
            await EXCHANGE_CLIENT.close() 
        except Exception:
            # 既に閉じているか、閉じられない場合も無視
            pass 
        finally:
            # 明示的にNoneに設定し、参照をクリアする (AttributeErrorの再発防止)
            EXCHANGE_CLIENT = None 

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
    if not EXCHANGE_CLIENT: return 0.0
    
    try:
        funding_rate = await EXCHANGE_CLIENT.fetch_funding_rate(symbol)
        return funding_rate.get('fundingRate', 0.0) if funding_rate else 0.0
    except Exception as e:
        return 0.0

async def update_symbols_by_volume():
    """CCXTを使用してOKXの出来高トップ30のUSDTペア銘柄を動的に取得・更新する"""
    global CURRENT_MONITOR_SYMBOLS, EXCHANGE_CLIENT, LAST_SUCCESSFUL_MONITOR_SYMBOLS
    if not EXCHANGE_CLIENT: return

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
        # スワップ市場で見つからなかった場合、現物市場を試す
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

async def fetch_ohlcv_for_macro(symbol: str, timeframe: str) -> Optional[pd.DataFrame]:
    """マクロコンテキスト (BTCドミナンスなど) 用のOHLCVデータを取得する"""
    
    if symbol == 'BTC-DOMINANCE':
        # TradingViewのティッカーに依存するため、CCXTではなく直接データプロバイダを使用することが多い
        # 現状は外部依存性を減らすため、一旦データ取得ロジックは省略
        return None 
    
    # 通常の取引所クライアントで取得
    ohlcv, status, _ = await fetch_ohlcv_with_fallback(CCXT_CLIENT_NAME, symbol, timeframe)
    
    if status != "Success":
        return None

    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms').dt.tz_localize(timezone.utc).dt.tz_convert(JST)
    df.set_index('timestamp', inplace=True)
    
    return df

# ====================================================================================
# TECHNICAL ANALYSIS ENGINE
# ====================================================================================

def calculate_technical_indicators(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """
    Pandas TAを使用してテクニカル指標を計算し、DataFrameに追加する。
    """
    if df.empty:
        return df

    # 1. 基礎的な指標
    df.ta.sma(length=LONG_TERM_SMA_LENGTH, append=True)
    df.ta.rsi(append=True)
    df.ta.macd(append=True)
    df.ta.cci(append=True)
    df.ta.adx(append=True)
    df.ta.stoch(append=True)
    df.ta.vwap(append=True)
    
    # 2. ボラティリティ指標 (BBANDS)
    df.ta.bbands(append=True)
    
    # 3. 構造的なS/R (Pivot Points)
    df.ta.pivot_fibonacci(append=True) # フィボナッチピボットを使用

    return df

def get_last_row_data(df: pd.DataFrame) -> Dict[str, Any]:
    """
    DataFrameの最後の行から必要なテクニカル指標を抽出する 
    """
    if df.empty:
        return {}

    # .iloc[-1] で最後の行（Series）を取得
    last_row = df.iloc[-1]
    
    # .get(key, default) を使用して、KeyErrorを回避し、np.nanをデフォルト値とする
    data = {
        'close': last_row.get('close', np.nan),
        'volume': last_row.get('volume', np.nan),
        'atr_value': last_row.get(f'ATR_{ta.get_defaults("atr").get("length")}', np.nan),
        'long_sma': last_row.get(f'SMA_{LONG_TERM_SMA_LENGTH}', np.nan),
        'rsi': last_row.get('RSI_14', np.nan),
        'stoch_k': last_row.get('STOCHk_14_3_3', np.nan),
        'stoch_d': last_row.get('STOCHd_14_3_3', np.nan),
        'macd_hist': last_row.get('MACDh_12_26_9', np.nan),
        'adx': last_row.get('ADX_14', np.nan),
        'cci': last_row.get('CCI_14_0.015', np.nan),
        'bb_width': last_row.get('BBP_20_2.0', np.nan), # BBPはBB%B
        'bb_high': last_row.get('BBU_20_2.0', np.nan),
        'bb_low': last_row.get('BBL_20_2.0', np.nan),
        'vwap': last_row.get('VWAP', np.nan),
        
        # Pivot Points
        'pp': last_row.get('P_F', np.nan),
        'r1': last_row.get('R1_F', np.nan),
        'r2': last_row.get('R2_F', np.nan),
        's1': last_row.get('S1_F', np.nan),
        's2': last_row.get('S2_F', np.nan),
    }
    
    # np.nanをPythonのNoneに変換しておく
    return {k: v if not pd.isna(v) else None for k, v in data.items()}

def get_prev_row_data(df: pd.DataFrame) -> Dict[str, Any]:
    """
    DataFrameの最後の行から2番目の行から必要なテクニカル指標を抽出する 
    """
    if df.shape[0] < 2:
        return {}

    # .iloc[-2] で最後の行から2番目の行（Series）を取得
    prev_row = df.iloc[-2]
    
    # .get(key, default) を使用して、KeyErrorを回避し、np.nanをデフォルト値とする
    data = {
        'close': prev_row.get('close', np.nan),
        'volume': prev_row.get('volume', np.nan),
        'macd_hist': prev_row.get('MACDh_12_26_9', np.nan),
    }
    
    # np.nanをPythonのNoneに変換しておく
    return {k: v if not pd.isna(v) else None for k, v in data.items()}

def analyze_single_timeframe(
    df: pd.DataFrame, 
    symbol: str, 
    timeframe: str, 
    funding_rate: float, 
    macro_context: Dict
) -> Dict[str, Any]:
    """
    単一の時間軸でシグナルを分析し、スコアと取引パラメータを計算する 
    """
    
    analysis_result = {
        'symbol': symbol,
        'timeframe': timeframe,
        'side': 'Neutral',
        'score': 0.50,
        'rr_ratio': DTS_RRR_DISPLAY,
        'price': 0.0,
        'entry': 0.0,
        'tp1': 0.0,
        'sl': 0.0,
        'entry_type': 'Limit',
        'tech_data': {},
        'macro_context': macro_context,
    }

    if df.shape[0] < LONG_TERM_SMA_LENGTH + 1:
        analysis_result['side'] = 'DataShortage'
        return analysis_result

    # 1. テクニカル指標の計算
    df = calculate_technical_indicators(df, timeframe)
    
    last_row = get_last_row_data(df)
    prev_row = get_prev_row_data(df)
    
    price = last_row.get('close')
    atr = last_row.get('atr_value')
    long_sma = last_row.get('long_sma')
    rsi = last_row.get('rsi')
    stoch_k = last_row.get('stoch_k')
    stoch_d = last_row.get('stoch_d')
    macd_hist = last_row.get('macd_hist')
    adx = last_row.get('adx')
    cci = last_row.get('cci')
    bb_width = last_row.get('bb_width')
    vwap = last_row.get('vwap')
    volume = last_row.get('volume')
    prev_volume = prev_row.get('volume')
    
    # 欠損値チェック (価格とATRが必須)
    if any(v is None for v in [price, atr, long_sma, rsi, adx, macd_hist, cci]):
        analysis_result['side'] = 'DataShortage'
        return analysis_result

    # 2. トレンド/レジームの決定
    if adx >= ADX_TREND_THRESHOLD:
        regime = 'Trend'
    elif bb_width is not None and bb_width < VOLATILITY_BB_PENALTY_THRESHOLD:
        regime = 'LowVol'
    else:
        regime = 'Chop'

    analysis_result['tech_data'].update({
        'price': price,
        'atr_value': atr,
        'long_term_trend': 'Bullish' if price > long_sma else ('Bearish' if price < long_sma else 'Neutral'),
        'rsi': rsi,
        'macd_hist': macd_hist,
        'adx': adx,
        'cci': cci,
        'regime': regime,
        'volume': volume,
        'bb_width': bb_width,
        'vwap': vwap,
        'pivot_points': {
            'pp': last_row.get('pp'), 'r1': last_row.get('r1'), 'r2': last_row.get('r2'),
            's1': last_row.get('s1'), 's2': last_row.get('s2'),
        }
    })

    score = BASE_SCORE
    side_long = False
    
    # ----------------------------------------------------
    # 3. コアロジック (RSI + MACD + CCI)
    # ----------------------------------------------------
    
    # Long Base: RSI (40-60) + CCI (< 0) -> MACDh 転換 ( prev < 0 and curr > 0)
    # 40-60 はモメンタムの強さを避ける（逆張り/押し目狙いの意図）
    # MACDh の転換は、短期的なモメンタムの反転を意味する
    
    # Long Condition
    is_long_base_rsi = (rsi < RSI_MOMENTUM_LOW) 
    is_long_cci = (cci < 0)
    is_long_macd_cross = (prev_row.get('macd_hist', -1.0) < 0) and (macd_hist > 0)

    # Short Condition
    is_short_base_rsi = (rsi > RSI_MOMENTUM_HIGH)
    is_short_cci = (cci > 0)
    is_short_macd_cross = (prev_row.get('macd_hist', 1.0) > 0) and (macd_hist < 0)

    if is_long_base_rsi and is_long_cci and is_long_macd_cross:
        score += 0.20 # コア確証
        side_long = True
        
    elif is_short_base_rsi and is_short_cci and is_short_macd_cross:
        score += 0.20 # コア確証
        side_long = False
        
    else:
        # コア条件が成立しない場合は Neutral
        analysis_result['side'] = 'Neutral'
        analysis_result['score'] = 0.50
        return analysis_result

    # ----------------------------------------------------
    # 4. フィルタリングとボーナス/ペナルティ
    # ----------------------------------------------------
    
    # 4.1. 長期トレンド逆行ペナルティ (4h足SMA50)
    long_term_trend_4h = macro_context.get('long_term_trend_4h', 'Neutral')
    long_term_reversal_penalty_value = 0.0

    if (side_long and long_term_trend_4h == 'Bearish') or \
       (not side_long and long_term_trend_4h == 'Bullish'):
        
        score -= LONG_TERM_REVERSAL_PENALTY
        long_term_reversal_penalty_value = LONG_TERM_REVERSAL_PENALTY
        analysis_result['tech_data']['long_term_reversal_penalty'] = True
        analysis_result['tech_data']['long_term_reversal_penalty_value'] = LONG_TERM_REVERSAL_PENALTY

    # 4.2. MACDモメンタム不一致ペナルティ
    # コア条件でMACDクロスを見たが、モメンタムが弱すぎる（MACDhが0付近、かつ直前と符号が逆転していない）場合はペナルティを課す
    macd_cross_valid = True
    macd_cross_penalty_value = 0.0
    
    if (side_long and macd_hist < 0) or (not side_long and macd_hist > 0):
        # ポジション方向とMACDhの符号が一致しない場合はモメンタム不一致
        score -= MACD_CROSS_PENALTY 
        macd_cross_valid = False
        macd_cross_penalty_value = MACD_CROSS_PENALTY
        
    analysis_result['tech_data']['macd_cross_valid'] = macd_cross_valid
    analysis_result['tech_data']['macd_cross_penalty_value'] = macd_cross_penalty_value

    # 4.3. VWAP乖離ペナルティ/ボーナス
    vwap_consistent = False
    if vwap is not None and price > 0:
        vwap_diff_percent = (price - vwap) / price
        if side_long and vwap_diff_percent < 0:
            vwap_consistent = True # ロングの際はVWAPより下にあると良い
        elif not side_long and vwap_diff_percent > 0:
            vwap_consistent = True # ショートの際はVWAPより上にあると良い

    analysis_result['tech_data']['vwap_consistent'] = vwap_consistent
    
    # 4.4. 出来高確証ボーナス
    volume_confirmation_bonus = 0.0
    volume_ratio = 1.0
    if volume is not None and prev_volume is not None and prev_volume > 0:
        volume_ratio = volume / prev_volume
        if volume_ratio >= VOLUME_CONFIRMATION_MULTIPLIER:
            volume_confirmation_bonus = 0.10
            score += volume_confirmation_bonus
            
    analysis_result['tech_data']['volume_confirmation_bonus'] = volume_confirmation_bonus
    analysis_result['tech_data']['volume_ratio'] = volume_ratio

    # 4.5. StochRSI 過熱ペナルティ (15m, 1hのみ)
    stoch_filter_penalty = 0.0
    if timeframe in ['15m', '1h'] and stoch_k is not None and stoch_d is not None:
        if side_long and stoch_k > 80: # ロングの際に買われすぎ
             stoch_filter_penalty = 0.10
             score -= stoch_filter_penalty
        elif not side_long and stoch_k < 20: # ショートの際に売られすぎ
             stoch_filter_penalty = 0.10
             score -= stoch_filter_penalty
             
    analysis_result['tech_data']['stoch_filter_penalty'] = stoch_filter_penalty

    # 4.6. 構造的 S/R (Pivot Points) ボーナス
    structural_pivot_bonus = 0.0
    pivot_points = analysis_result['tech_data']['pivot_points']
    
    if pivot_points.get('pp') is not None:
        if side_long:
            # ロングの場合、価格がS1/S2に近づいているとボーナス
            s1_price = pivot_points.get('s1')
            s2_price = pivot_points.get('s2')
            
            # S1より下、またはS1-PPの間に価格がある場合を考慮
            if price < s1_price:
                 structural_pivot_bonus = 0.10
                 score += structural_pivot_bonus
            elif price < pivot_points.get('pp') and price > s1_price * 0.999: # S1の近く
                 structural_pivot_bonus = 0.05
                 score += structural_pivot_bonus
            
        elif not side_long:
            # ショートの場合、価格がR1/R2に近づいているとボーナス
            r1_price = pivot_points.get('r1')
            r2_price = pivot_points.get('r2')
            
            # R1より上、またはR1-PPの間に価格がある場合を考慮
            if price > r1_price:
                 structural_pivot_bonus = 0.10
                 score += structural_pivot_bonus
            elif price > pivot_points.get('pp') and price < r1_price * 1.001: # R1の近く
                 structural_pivot_bonus = 0.05
                 score += structural_pivot_bonus
                 
    analysis_result['tech_data']['structural_pivot_bonus'] = structural_pivot_bonus


    # 4.7. 資金調達率 (Funding Rate) バイアスフィルター
    funding_rate_bonus_value = 0.0
    analysis_result['tech_data']['funding_rate_value'] = funding_rate
    
    if abs(funding_rate) >= FUNDING_RATE_THRESHOLD:
        if funding_rate > 0 and not side_long:
            # 資金調達率がプラス（ロングの過密）でショートの場合 -> ボーナス
            funding_rate_bonus_value = FUNDING_RATE_BONUS_PENALTY
            score += funding_rate_bonus_value
        elif funding_rate < 0 and side_long:
            # 資金調達率がマイナス（ショートの過密）でロングの場合 -> ボーナス
            funding_rate_bonus_value = FUNDING_RATE_BONUS_PENALTY
            score += funding_rate_bonus_value
        else:
            # 資金調達率と方向が一致する場合 -> ペナルティ
            funding_rate_bonus_value = -FUNDING_RATE_BONUS_PENALTY
            score -= FUNDING_RATE_BONUS_PENALTY
            
    analysis_result['tech_data']['funding_rate_bonus_value'] = funding_rate_bonus_value
    
    # 4.8. BTC Dominance Bias フィルター (Altcoinのみ)
    dominance_trend = macro_context.get('dominance_trend', 'Neutral')
    dominance_bias_bonus_value = 0.0
    
    if symbol != 'BTC-USDT':
        if dominance_trend == 'Bearish' and side_long:
            # ドミナンスが下落傾向（アルトに資金流入）でロング -> ボーナス
            dominance_bias_bonus_value = DOMINANCE_BIAS_BONUS_PENALTY
            score += dominance_bias_bonus_value
        elif dominance_trend == 'Bullish' and not side_long:
            # ドミナンスが上昇傾向（BTCに資金集中）でショート -> ボーナス (相対的な弱さ)
            dominance_bias_bonus_value = DOMINANCE_BIAS_BONUS_PENALTY
            score += dominance_bias_bonus_value
        else:
            # トレンドに逆行している場合 -> ペナルティ
            dominance_bias_bonus_value = -DOMINANCE_BIAS_BONUS_PENALTY / 2.0 # ペナルティは少し軽め
            score += dominance_bias_bonus_value

    analysis_result['tech_data']['dominance_trend'] = dominance_trend
    analysis_result['tech_data']['dominance_bias_bonus_value'] = dominance_bias_bonus_value

    # 5. 結果の決定と取引パラメータの計算
    
    # 最終スコアを0.0から1.0に制限
    score = max(0.01, min(1.0, score))
    analysis_result['score'] = score
    
    if score < SIGNAL_THRESHOLD:
        analysis_result['side'] = 'Neutral'
        return analysis_result

    analysis_result['side'] = 'ロング' if side_long else 'ショート'
    
    # DTSパラメータ (エントリー、SL) の計算
    atr_sl_width = atr * ATR_TRAIL_MULTIPLIER
    
    # 構造的SL/TPの設定 (Pivot Points S1, R1を考慮)
    structural_sl_used = False
    
    if side_long:
        # ロングの場合
        # エントリー: 直近の安値のわずかに上 (Limit Buy)
        entry_price = last_row.get('low', price) + (atr * 0.5) 
        
        # SL候補: ATR基準
        sl_candidate_atr = entry_price - atr_sl_width
        
        # SL候補: 構造的S/R (S1, S2) を使用
        s1_price = pivot_points.get('s1')
        s2_price = pivot_points.get('s2')
        
        sl_price = sl_candidate_atr
        
        # S1がATR基準のSLよりも深く、かつ妥当な範囲内であればS1付近を使用
        if s1_price is not None and s1_price < sl_candidate_atr and s1_price > (price * 0.95):
             sl_price = s1_price - (atr * 0.5) # S1に0.5ATRのバッファを追加
             structural_sl_used = True
        elif s2_price is not None and s2_price < sl_candidate_atr and s2_price > (price * 0.95):
             sl_price = s2_price - (atr * 0.5) # S2に0.5ATRのバッファを追加
             structural_sl_used = True

        # TPはDTSの目標RRRに基づいて設定 (あくまで目標)
        tp_price = entry_price + (abs(entry_price - sl_price) * DTS_RRR_DISPLAY)
        
        # エントリータイプは一旦価格を割り込ませてから反発を狙うLimitとする
        analysis_result['entry_type'] = 'Limit'

    else:
        # ショートの場合
        # エントリー: 直近の高値のわずかに下 (Limit Sell)
        entry_price = last_row.get('high', price) - (atr * 0.5)
        
        # SL候補: ATR基準
        sl_candidate_atr = entry_price + atr_sl_width
        
        # SL候補: 構造的S/R (R1, R2) を使用
        r1_price = pivot_points.get('r1')
        r2_price = pivot_points.get('r2')
        
        sl_price = sl_candidate_atr

        # R1がATR基準のSLよりも深く、かつ妥当な範囲内であればR1付近を使用
        if r1_price is not None and r1_price > sl_candidate_atr and r1_price < (price * 1.05):
             sl_price = r1_price + (atr * 0.5) # R1に0.5ATRのバッファを追加
             structural_sl_used = True
        elif r2_price is not None and r2_price > sl_candidate_atr and r2_price < (price * 1.05):
             sl_price = r2_price + (atr * 0.5) # R2に0.5ATRのバッファを追加
             structural_sl_used = True

        # TPはDTSの目標RRRに基づいて設定 (あくまで目標)
        tp_price = entry_price - (abs(entry_price - sl_price) * DTS_RRR_DISPLAY)
        
        # エントリータイプは一旦価格を突き抜けさせてから反発を狙うLimitとする
        analysis_result['entry_type'] = 'Limit'

    # パラメータの更新
    analysis_result['price'] = price
    analysis_result['entry'] = entry_price
    analysis_result['sl'] = sl_price
    analysis_result['tp1'] = tp_price 
    analysis_result['rr_ratio'] = DTS_RRR_DISPLAY # RRRはDTSの目標値
    analysis_result['tech_data']['structural_sl_used'] = structural_sl_used
    
    # リスク幅が小さすぎる場合は無効化
    if abs(entry_price - sl_price) < (atr * 0.5):
         analysis_result['side'] = 'Neutral'
         analysis_result['score'] = 0.50
         
    return analysis_result

async def fetch_macro_context(monitor_symbols: List[str]) -> Dict:
    """
    BTCドミナンスやFGIなどのマクロ環境の情報を取得・分析する
    """
    context = {
        'long_term_trend_4h': 'Neutral', # BTC/USDT 4h足の長期トレンド
        'dominance_trend': 'Neutral',   # BTC Dominance のトレンド
        'sentiment_fgi_proxy': 0.0,     # FGIの代理スコア (-1.0 to 1.0)
    }

    # 1. BTC/USDT 4h足の長期トレンドを取得
    btc_ohlcv, status, _ = await fetch_ohlcv_with_fallback(CCXT_CLIENT_NAME, 'BTC-USDT', '4h')
    
    if status == 'Success' and len(btc_ohlcv) >= LONG_TERM_SMA_LENGTH + 1:
        btc_df = pd.DataFrame(btc_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        btc_df['timestamp'] = pd.to_datetime(btc_df['timestamp'], unit='ms').dt.tz_localize(timezone.utc).dt.tz_convert(JST)
        btc_df.set_index('timestamp', inplace=True)
        
        # SMAを計算
        btc_df.ta.sma(length=LONG_TERM_SMA_LENGTH, append=True)
        
        last_row = get_last_row_data(btc_df)
        btc_price = last_row.get('close')
        btc_long_sma = last_row.get('long_sma')
        
        if btc_price is not None and btc_long_sma is not None and btc_price > 0:
            if btc_price > btc_long_sma:
                context['long_term_trend_4h'] = 'Bullish'
            elif btc_price < btc_long_sma:
                context['long_term_trend_4h'] = 'Bearish'

    # 2. BTC Dominanceのトレンドを取得
    # 現時点では、簡易的なFGIの代理スコア (-1.0 to 1.0) のみを返す（BTCトレンドから仮定）
    if context['long_term_trend_4h'] == 'Bullish':
        context['sentiment_fgi_proxy'] = 0.30 # 楽観
    elif context['long_term_trend_4h'] == 'Bearish':
        context['sentiment_fgi_proxy'] = -0.30 # 悲観
    else:
        context['sentiment_fgi_proxy'] = 0.0
        
    # ドミナンスの簡易トレンド (BTC-USDTとALTのパフォーマンス差から暫定的に決定)
    if context['sentiment_fgi_proxy'] > 0.1:
         context['dominance_trend'] = 'Bullish'
    elif context['long_term_trend_4h'] == 'Bullish' and context['sentiment_fgi_proxy'] < 0.1:
         # BTCが上昇トレンドだがFGI代理スコアが伸びていない = アルトの相対的な強さ (暫定ロジック)
         context['dominance_trend'] = 'Bearish'
    elif context['sentiment_fgi_proxy'] < -0.1:
         context['dominance_trend'] = 'Bearish'
    else:
         context['dominance_trend'] = 'Neutral'

    logging.info(f"マクロコンテキストを更新: 4h BTCトレンド={context['long_term_trend_4h']}, ドミナンス={context['dominance_trend']}")
    return context


async def analyze_all_timeframes(symbol: str, macro_context: Dict) -> List[Dict]:
    """
    全ての時間軸のOHLCVデータを取得し、分析を統合する
    """
    
    timeframes = ['15m', '1h', '4h']
    tasks = []
    
    # 資金調達率を事前に取得
    funding_rate = await fetch_funding_rate(symbol)
    
    # OHLCVデータの取得タスクを生成
    for tf in timeframes:
        tasks.append(fetch_ohlcv_with_fallback(CCXT_CLIENT_NAME, symbol, tf))

    ohlcv_results = await asyncio.gather(*tasks)
    
    all_signals = []
    
    for (ohlcv, status, client), tf in zip(ohlcv_results, timeframes):
        if status != 'Success':
            all_signals.append({
                'symbol': symbol,
                'timeframe': tf,
                'side': status,
                'score': 0.50,
                'rr_ratio': DTS_RRR_DISPLAY,
            })
            continue

        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms').dt.tz_localize(timezone.utc).dt.tz_convert(JST)
        df.set_index('timestamp', inplace=True)
        
        signal = analyze_single_timeframe(df, symbol, tf, funding_rate, macro_context)
        all_signals.append(signal)

    return all_signals

# ====================================================================================
# MAIN LOOP & SIGNAL PROCESSING
# ====================================================================================

async def process_signals(all_signals: List[Dict]) -> List[Dict]:
    """
    全銘柄・全時間軸のシグナルを統合し、最適な取引シグナルをフィルタリングする
    """
    
    # 1. 有効なシグナルのみを抽出
    valid_signals = [
        s for s in all_signals 
        if s.get('side') not in ["DataShortage", "ExchangeError", "Neutral"]
        and s.get('score', 0.50) >= SIGNAL_THRESHOLD
    ]

    # 2. 複数の時間軸で同じ方向のシグナルが出ているものを優先
    symbol_scores: Dict[str, Dict[str, Any]] = {}

    for signal in valid_signals:
        symbol = signal['symbol']
        timeframe = signal['timeframe']
        score = signal['score']
        
        # 銘柄ごとの統合スコアを計算
        if symbol not in symbol_scores:
            symbol_scores[symbol] = {
                'total_score': 0.0,
                'count': 0,
                'best_signal': signal,
            }
        
        # 同じ銘柄内で最もスコアの高いシグナルを採用
        if score > symbol_scores[symbol]['best_signal']['score']:
             symbol_scores[symbol]['best_signal'] = signal

        # スコアを加算
        symbol_scores[symbol]['total_score'] += score
        symbol_scores[symbol]['count'] += 1

    # 3. 統合スコアでランキング
    ranked_signals = sorted(
        symbol_scores.values(),
        key=lambda x: (
            x['total_score'], 
            x['count'],
            x['best_signal']['rr_ratio'] # RRRも考慮
        ), 
        reverse=True
    )
    
    final_signals = []
    
    for i, item in enumerate(ranked_signals[:TOP_SIGNAL_COUNT]):
        best_signal_data = item['best_signal']
        symbol = best_signal_data['symbol']
        
        # クールダウン期間のチェック
        last_notified = TRADE_NOTIFIED_SYMBOLS.get(symbol, 0)
        if (time.time() - last_notified) < TRADE_SIGNAL_COOLDOWN:
            logging.info(f"⏰ {symbol} はクールダウン期間中のためスキップします。")
            continue

        # 4. メッセージを整形して通知
        rank = i + 1
        
        # 該当銘柄の全シグナルを取得
        integrated_signals = [s for s in all_signals if s['symbol'] == symbol]
        
        message = format_integrated_analysis_message(symbol, integrated_signals, rank)
        
        if message and send_telegram_html(message):
            TRADE_NOTIFIED_SYMBOLS[symbol] = time.time()
            final_signals.append(best_signal_data)
            
    return final_signals

async def main_loop():
    """
    メインの監視ループ - Stability Fix (v17.0.6)適用
    """
    global LAST_UPDATE_TIME, LAST_SUCCESS_TIME, LAST_ANALYSIS_SIGNALS, GLOBAL_MACRO_CONTEXT
    
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == 'YOUR_TELEGRAM_TOKEN':
        logging.error("❌ Telegram Token が設定されていません。環境変数を確認してください。")
        return

    await initialize_ccxt_client()

    while True:
        try:
            logging.info(f"\n--- 🚀 Apex BOT v17.0.6 - 監視ループ開始 (時刻: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')} JST) ---") # バージョン更新
            
            # 1. 銘柄リストの動的更新 (低頻度)
            if time.time() - LAST_UPDATE_TIME > 60 * 60 * 4: # 4時間に1回
                 logging.info("📡 出来高に基づいて監視銘柄リストを更新します...")
                 await update_symbols_by_volume()
                 LAST_UPDATE_TIME = time.time()
                 
            # 2. マクロコンテキストの取得
            GLOBAL_MACRO_CONTEXT = await fetch_macro_context(CURRENT_MONITOR_SYMBOLS)
            
            # 3. 全銘柄の分析タスクを作成
            analysis_tasks = []
            for symbol in CURRENT_MONITOR_SYMBOLS:
                 await asyncio.sleep(REQUEST_DELAY_PER_SYMBOL) # 負荷軽減のための遅延
                 analysis_tasks.append(analyze_all_timeframes(symbol, GLOBAL_MACRO_CONTEXT))

            logging.info(f"⏳ {len(analysis_tasks)} 銘柄の分析を開始します...")
            all_results_nested = await asyncio.gather(*analysis_tasks)
            
            # 結果をフラット化
            all_signals: List[Dict] = [signal for sublist in all_results_nested for signal in sublist]
            
            # 4. シグナル処理と通知
            logging.info("🧠 分析結果を統合し、シグナルを処理します...")
            notified_signals = await process_signals(all_signals)
            
            LAST_ANALYSIS_SIGNALS = all_signals
            LAST_SUCCESS_TIME = time.time()
            logging.info(f"✅ 監視ループ完了。{len(notified_signals)} 件のシグナルを通知しました。")
            
            # 5. インターバル待機
            await asyncio.sleep(LOOP_INTERVAL)

        except KeyboardInterrupt:
            logging.info("メインループを停止します (Keyboard Interrupt)。")
            break
        except Exception as e:
            error_name = type(e).__name__
            logging.error(f"メインループで致命的なエラー: {error_name}")
            
            # --- FIX: Attribute Error または接続関連の致命的なエラーが発生した場合、クライアントを再初期化して回復を試みる ---
            if error_name in ['AttributeError', 'ConnectionError', 'TimeoutError']:
                logging.warning("⚠️ クライアントの不安定性を検知しました。再初期化を試行します...")
                await initialize_ccxt_client() 
                # NEW FIX 2: クライアント再初期化後、短い待機時間を挿入して、新しい aiohttp セッションが完全に安定するのを待つ
                await asyncio.sleep(3) 
            # --------------------------------------------------------------------------------------------------------

            await asyncio.sleep(60)


# ====================================================================================
# FASTAPI SETUP
# ====================================================================================

app = FastAPI(title="Apex BOT API", version="v17.0.6 - CCXT Stability Fix") # バージョン更新

@app.on_event("startup")
async def startup_event():
    logging.info("🚀 Apex BOT v17.0.6 Startup initializing...") # バージョン更新
    asyncio.create_task(main_loop())

@app.on_event("shutdown")
async def shutdown_event():
    global EXCHANGE_CLIENT
    if EXCHANGE_CLIENT:
        # シャットダウン時にも安全にセッションを閉じる
        try:
            await EXCHANGE_CLIENT.close()
            logging.info("CCXTクライアントをシャットダウンしました。")
        except Exception:
            logging.warning("CCXTクライアントのシャットダウン中にエラーが発生しました。")


@app.get("/status")
def get_status():
    status_msg = {
        "status": "ok",
        "bot_version": "v17.0.6 - CCXT Stability Fix", # バージョン更新
        "last_success_time_utc": datetime.fromtimestamp(LAST_SUCCESS_TIME, tz=timezone.utc).isoformat() if LAST_SUCCESS_TIME else "N/A",
        "current_client": CCXT_CLIENT_NAME,
        "monitoring_symbols": len(CURRENT_MONITOR_SYMBOLS),
        "last_signals_count": len(LAST_ANALYSIS_SIGNALS)
    }
    return JSONResponse(content=status_msg)

@app.head("/")
@app.get("/")
def home_view():
    return JSONResponse(content={"message": "Apex BOT is running (v17.0.6)"})

if __name__ == "__main__":
    # 環境変数からポート番号を取得し、uvicornを起動
    port = int(os.environ.get("PORT", 8000))
    # Note: main_render:app は、このファイル名:FastAPI変数名
    uvicorn.run("main_render:app", host="0.0.0.0", port=port, log_level="info")
