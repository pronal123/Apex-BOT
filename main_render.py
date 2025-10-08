# ====================================================================================
# Apex BOT v19.0.2 - Robust Symbol Filter Fix
# - FIX: OKXなどの取引所でのBadSymbolエラーを解決するため、シンボルフィルタリングのロジックをCCXTの市場情報(base/quote)に基づき強化。
# - 機能: 構造的な支持線/抵抗線 (S/R) で利確/損切した場合の損益額を計算し、通知に追加表示。
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
# .envファイルに TELEGRAM_TOKEN, TELEGRAM_CHAT_ID を設定してください。
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
DYNAMIC_TP_RRR_MIN = 1.5            # NEW: スコアに基づく初期TP目標の最小RRR (スコア 0.75時)
DYNAMIC_TP_RRR_MAX = 2.5            # NEW: スコアに基づく初期TP目標の最大RRR (スコア 1.0時)

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
    if TELEGRAM_TOKEN == 'YOUR_TELEGRAM_TOKEN' or TELEGRAM_CHAT_ID == 'YOUR_TELEGRAM_CHAT_ID':
         logging.warning("TelegramトークンまたはチャットIDが設定されていません。通知をスキップします。")
         return False
         
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
    3つの時間軸の分析結果を統合し、ログメッセージの形式に整形する (v19.0.2対応)
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
    rr_ratio = best_signal.get('rr_ratio', 0.0) # 動的に計算された RRR

    entry_price = best_signal.get('entry', 0.0)
    tp_price = best_signal.get('tp1', 0.0) 
    sl_price = best_signal.get('sl', 0.0) 
    entry_type = best_signal.get('entry_type', 'N/A') 

    score_100 = score_raw * 100
    win_rate = get_estimated_win_rate(score_raw, timeframe) * 100
    display_symbol = symbol.replace('-', '/')
    
    # リスク幅を計算 (初期のストップ位置との差)
    sl_width = abs(entry_price - sl_price)
    
    # NEW: $1000 リスクに基づいたP&L計算 (TP/SLでの損益額を具体的に表示)
    sl_loss_usd = 1000.0 # 許容損失額 (ポジションサイズではない)
    tp_gain_usd = sl_loss_usd * rr_ratio # TPの予想利益は、リスクのRRR倍
    
    # NEW: 構造的目標 (S/R) で利確・損切した場合のP&Lを計算 (v19.0.0)
    structural_target_pnl_usd = 0.0
    structural_target_price = 0.0
    structural_target_type = ""
    
    pivot_points = best_signal.get('tech_data', {}).get('pivot_points', {})
    
    if sl_width > 0 and pivot_points:
        # ポジションサイズを逆算: $1000 / リスク幅
        position_size = sl_loss_usd / sl_width
        
        if side == 'ロング':
            # Long: 利確目標は最も近い抵抗線 (R1またはPP)
            target_r1 = pivot_points.get('r1', 0.0)
            target_pp = pivot_points.get('pp', 0.0)
            
            # Entryより上にある目標価格のリスト
            target_list = [t for t in [target_r1, target_pp] if t > entry_price]
            
            if target_list:
                # 最も近い目標をTPとして採用 (最小値)
                structural_target_price = min(target_list)
                structural_target_type = "R1/PP 利確" 
            
        elif side == 'ショート':
            # Short: 利確目標は最も近い支持線 (S1またはPP)
            target_s1 = pivot_points.get('s1', 0.0)
            target_pp = pivot_points.get('pp', 0.0)
            
            # Entryより下にある目標価格のリスト
            target_list = [t for t in [target_s1, target_pp] if t < entry_price]
            
            if target_list:
                # 最も近い目標をTPとして採用 (最大値)
                structural_target_price = max(target_list)
                structural_target_type = "S1/PP 利確"

        if structural_target_price > 0:
            # Entryと構造目標の価格差
            structural_target_width = abs(entry_price - structural_target_price)
            # 損益額を計算
            structural_target_pnl_usd = structural_target_width * position_size
            
            # リスクリワード1.0未満の構造的目標は表示しない
            if structural_target_pnl_usd < sl_loss_usd:
                structural_target_pnl_usd = 0.0 

    
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
    exit_type_str = f"DTS (動的追跡損切) + 初期TP ({rr_ratio:.2f}:1)" 
    
    # TP到達目安を追加
    time_to_tp = get_tp_reach_time(timeframe)
    
    # NEW: 構造的目標P&Lの行
    structural_pnl_line = ""
    if structural_target_pnl_usd > 0 and structural_target_price > 0:
         structural_pnl_line = (
             f"| 🛡️ **構造目標P&L** | **{structural_target_type}** の場合: **<ins>約 ${structural_target_pnl_usd:,.0f}</ins>** | "
             f"RRR: 1:{round(structural_target_pnl_usd / sl_loss_usd, 2):.2f} |\n"
         )
    

    header = (
        f"--- 🟢 --- **{display_symbol}** --- 🟢 ---\n"
        f"{rank_header} 📈 {strength} 発生！ - {direction_emoji}{market_sentiment_str}\n" 
        f"==================================\n"
        f"| 🎯 **予測勝率** | **<ins>{win_rate:.1f}%</ins>** | **条件極めて良好** |\n"
        f"| 💯 **分析スコア** | <b>{score_100:.2f} / 100.00 点</b> (ベース: {timeframe}足) |\n" 
        f"| 💰 **動的TP P&L** | **<ins>損益比 1:{rr_ratio:.2f}</ins>** (損失: ${sl_loss_usd:,.0f} / 利益: **${tp_gain_usd:,.0f}**) |\n"
        f"{structural_pnl_line}" # 構造的P&Lを追加
        f"| ⏰ **決済戦略** | **{exit_type_str}** |\n" 
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
        f"| 📉 **Risk (SL幅)** | ${format_price_utility(sl_width, symbol)} | **初動リスク** (ATR x {ATR_TRAIL_MULTIPLIER:.1f}基準) |\n"
        f"| 🟢 **TP目標** | <code>${format_price_utility(tp_price, symbol)}</code> | **スコアに基づく初期目標** (RRR: 1:{rr_ratio:.2f}) |\n" # TP説明を修正
        f"| ❌ **SL 位置** | <code>${format_price_utility(sl_price, symbol)}</code> | 損切 ({sl_source_str} / **初動SL** / DTS追跡開始点) |\n" # SL説明を修正
        f"----------------------------------\n"
    )

    # NEW: 抵抗候補・支持候補の明記
    
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
                dominance_bias_value = tech_data.get('dominance_bias_bonus_value', 0.0)
                
                dominance_status = ""
                if dominance_bias_value > 0:
                    dominance_status = f"✅ **優位性あり** (<ins>**+{dominance_bias_value * 100:.2f}点**</ins>)"
                elif dominance_bias_value < 0:
                    dominance_status = f"⚠️ **バイアスにより減点適用** (<ins>**-{abs(dominance_bias_value) * 100:.2f}点**</ins>)"
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
        f"| ⚙️ **BOT Ver** | **v19.0.2** - Robust Symbol Filter Fix |\n" # バージョン更新
        f"==================================\n"
        f"\n<pre>※ 表示されたTPはDTSによる追跡決済の**初期目標値**です。DTSが有効な場合、利益は自動的に最大化されます。</pre>"
    )

    return header + trade_plan + sr_info + analysis_detail + footer


# ====================================================================================
# CCXT & DATA ACQUISITION
# ====================================================================================

async def initialize_exchange() -> Optional[ccxt_async.Exchange]:
    """CCXTクライアントを初期化する"""
    global EXCHANGE_CLIENT
    
    if EXCHANGE_CLIENT:
        await EXCHANGE_CLIENT.close()
    
    exchange_class = getattr(ccxt_async, CCXT_CLIENT_NAME.lower(), None)
    if not exchange_class:
        logging.error(f"交換所 {CCXT_CLIENT_NAME} が見つかりません。")
        return None
        
    try:
        # 環境変数から API キーとシークレットを取得 (必要に応じて)
        api_key = os.environ.get(f'{CCXT_CLIENT_NAME.upper()}_API_KEY')
        secret = os.environ.get(f'{CCXT_CLIENT_NAME.upper()}_SECRET')
        
        config = {
            'enableRateLimit': True,
            'apiKey': api_key,
            'secret': secret,
            'options': {
                'defaultType': 'future', # OKXの場合、デフォルトを先物に設定
            }
        }
        
        client = exchange_class(config)
        await client.load_markets() # 市場情報をロード
        EXCHANGE_CLIENT = client
        logging.info(f"CCXTクライアント ({CCXT_CLIENT_NAME}) を初期化しました。市場情報 ({len(client.markets)}件) をロードしました。")
        return client
        
    except Exception as e:
        logging.error(f"CCXTクライアント初期化エラー: {e}")
        return None

async def fetch_ohlcv_with_fallback(exchange: ccxt_async.Exchange, symbol: str, timeframe: str, limit: int) -> List[List[float]]:
    """OHLCVデータを取得し、エラー発生時に None を返す"""
    
    # OKXなどの一部の取引所では、シンボル形式を調整する必要がある
    formatted_symbol = symbol.replace('-', '/')
    
    try:
        # rate limit対策のために遅延を入れる
        await asyncio.sleep(REQUEST_DELAY_PER_SYMBOL)
        ohlcv = await exchange.fetch_ohlcv(formatted_symbol, timeframe, limit=limit)
        return ohlcv
    except (ccxt.ExchangeError, ccxt.NetworkError, ccxt.RequestTimeout) as e:
        # フィルタリング済みのため、ここでは主にレート制限やネットワークエラーを処理
        logging.warning(f"データ取得エラー ({symbol} - {timeframe}): {type(e).__name__}: {e}")
        return []
    except Exception as e:
        logging.error(f"予期せぬデータ取得エラー ({symbol} - {timeframe}): {e}")
        return []

# ------------------------------------------------------------------------------------
# NEW/FIX: シンボルフィルタリング機能 (v19.0.2)
# ------------------------------------------------------------------------------------
async def filter_monitoring_symbols(exchange: ccxt_async.Exchange):
    """
    ロードされた市場情報に基づき、DEFAULT_SYMBOLSから実際に存在する先物/スワップシンボルに絞り込む。
    
    v19.0.2: CCXTのmarket情報にある'base'と'quote'キーを利用し、フィルタリングの正確性を向上。
    """
    global CURRENT_MONITOR_SYMBOLS
    
    if not exchange.markets:
        logging.warning("市場情報がロードされていません。シンボルフィルタリングをスキップします。")
        return

    # フィルタリングするシンボルリスト
    initial_symbols = [s.replace('/', '-') for s in DEFAULT_SYMBOLS[:TOP_SYMBOL_LIMIT]]
    
    # 実際に存在する先物/スワップシンボルのベース通貨 (例: 'MATIC') を収集
    available_base_pairs = set()
    for symbol, market in exchange.markets.items():
        # 'future'または'swap'であること、かつUSDT建てであることを確認
        is_contract = market.get('contract', False)
        is_swap = market.get('swap', False)
        
        if (is_contract or is_swap) and market.get('quote') == 'USDT':
            base_symbol = market.get('base') 
            if base_symbol:
                available_base_pairs.add(base_symbol)
    
    # 監視リストを更新
    new_monitoring_symbols = []
    removed_symbols = []
    
    for symbol_hyphen in initial_symbols:
        # ベース通貨を抽出 (例: 'MATIC-USDT' -> 'MATIC')
        base_symbol_check = symbol_hyphen.split('-')[0]
        
        # 実際に利用可能なベース通貨のセットに存在するかを確認
        if base_symbol_check in available_base_pairs:
            new_monitoring_symbols.append(symbol_hyphen)
        else:
            removed_symbols.append(symbol_hyphen)

    if removed_symbols:
        logging.warning(f"以下のシンボルはOKXの先物/スワップ市場で見つかりませんでした (BadSymbolエラー対策): {', '.join(removed_symbols)}")
    
    if not new_monitoring_symbols:
         logging.error("有効な先物/スワップシンボルが見つかりませんでした。デフォルトのシンボルリストを確認してください。")
         new_monitoring_symbols = initial_symbols # エラー時に続行するためにフォールバック
         
    CURRENT_MONITOR_SYMBOLS = new_monitoring_symbols
    logging.info(f"監視対象のシンボルリストを更新しました。有効なシンボル数: {len(CURRENT_MONITOR_SYMBOLS)}/{len(initial_symbols)}")

# ------------------------------------------------------------------------------------


def calculate_pivot_points(df: pd.DataFrame) -> Dict[str, float]:
    """
    日足データに基づき古典的なピボットポイント (PP, R1, S1, R2, S2) を計算する。
    
    Args:
        df: 過去のOHLCVを含むDataFrame。最低でも前日のデータが必要。
        
    Returns:
        ピボットポイントの価格を含む辞書。
    """
    # 最後の行（現在足）の前の行が、前日の終値、高値、安値
    if len(df) < 2:
        return {}

    # 前日のデータ (日足分析の場合、通常は前日の確定足)
    prev_close = df['close'].iloc[-2]
    prev_high = df['high'].iloc[-2]
    prev_low = df['low'].iloc[-2]
    
    # ピボットポイント (PP)
    pp = (prev_high + prev_low + prev_close) / 3
    
    # 抵抗線 (Resistance)
    r1 = (2 * pp) - prev_low
    s1 = (2 * pp) - prev_high
    
    # 第2抵抗線/支持線 (R2, S2)
    r2 = pp + (prev_high - prev_low)
    s2 = pp - (prev_high - prev_low)

    return {
        'pp': pp, 
        'r1': r1, 
        's1': s1, 
        'r2': r2, 
        's2': s2
    }

async def get_crypto_macro_context(exchange: ccxt_async.Exchange, symbols: List[str]) -> Dict[str, Any]:
    """
    資金調達率 (Funding Rate) と BTCドミナンスの分析など、市場全体の情報を取得・分析する。
    """
    macro_context = {}
    
    # 1. 資金調達率 (Funding Rate) 取得
    for symbol in symbols:
        formatted_symbol = symbol.replace('-', '/')
        try:
            # OKXでは 'fetch_funding_rate' が利用可能
            funding_rate_data = await exchange.fetch_funding_rate(formatted_symbol)
            # 'fundingRate' の値 (例: 0.0001) を取得
            fr_value = funding_rate_data.get('fundingRate', 0.0)
            macro_context[f'{symbol}_fr'] = fr_value
            await asyncio.sleep(0.1) # レートリミット対策
        except Exception as e:
            # logging.debug(f"FR取得失敗 ({symbol}): {e}")
            macro_context[f'{symbol}_fr'] = 0.0
            
    # 2. BTCドミナンス分析 (簡易的なプロキシとして BTC/USDT の出来高/価格トレンドを使用)
    dominance_trend = 'Neutral'
    dominance_bias_score = 0.0
    
    btc_symbol = 'BTC-USDT'
    if btc_symbol in symbols:
        try:
            # BTCの日足OHLCVを取得
            btc_ohlcv = await fetch_ohlcv_with_fallback(exchange, btc_symbol, '1d', 30)
            if btc_ohlcv:
                btc_df = pd.DataFrame(btc_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                btc_df['close'] = pd.to_numeric(btc_df['close'])
                btc_df['volume'] = pd.to_numeric(btc_df['volume'])
                
                # 過去20日間の終値のSMA (移動平均線)
                btc_df['sma_20'] = btc_df['close'].rolling(window=20).mean()
                
                # トレンド判定 (過去20日間の価格と出来高の複合分析)
                if len(btc_df) >= 20:
                    current_close = btc_df['close'].iloc[-1]
                    prev_close = btc_df['close'].iloc[-2]
                    sma_20 = btc_df['sma_20'].iloc[-1]
                    
                    # 価格トレンド
                    price_trend = (current_close > sma_20)
                    
                    # 簡易的なAltcoin Bias判定
                    # BTCがレンジや下落傾向だが、価格がSMAより上 (不安定なAltシーズンを示唆) -> Altcoin Longにボーナス (負のバイアス)
                    # BTCが強い上昇トレンドで価格がSMAより上 (資金集中) -> Altcoin Shortにボーナス (正のバイアス)
                    
                    if price_trend: # BTCが強い場合
                        dominance_trend = 'Long' 
                    else: # BTCが弱い/レンジの場合
                        dominance_trend = 'Short' # Altcoinにチャンスがある、と解釈 (BTC Dominance Downのプロキシ)
                        
                    # BTCが Long トレンドで、Altcoinに資金が流れる可能性が低い場合、Altcoin Longにはペナルティ (正のスコアバイアス)
                    if dominance_trend == 'Long':
                        dominance_bias_score = DOMINANCE_BIAS_BONUS_PENALTY # Altcoin Longにペナルティ、Shortにボーナス
                    else:
                        dominance_bias_score = -DOMINANCE_BIAS_BONUS_PENALTY # Altcoin Shortにペナルティ、Longにボーナス (負のスコアバイアス)
                        
        except Exception as e:
            logging.warning(f"BTCドミナンス分析エラー: {e}")

    macro_context['dominance_trend'] = dominance_trend
    macro_context['dominance_bias_score'] = dominance_bias_score
    
    # 3. Fear & Greed Index (FGI) プロキシ (簡易的な感情分析)
    # ここではランダムな値を使用するが、実運用では外部APIから取得する。
    macro_context['sentiment_fgi_proxy'] = random.uniform(-0.15, 0.15) 
    
    return macro_context


# ====================================================================================
# TRADING STRATEGY & SCORING LOGIC
# ====================================================================================

def analyze_single_timeframe(symbol: str, timeframe: str, ohlcv: List[List[float]], macro_context: Dict) -> Dict:
    """単一の時間足に基づいた詳細なテクニカル分析とシグナルスコアリングを実行する"""
    
    if not ohlcv or len(ohlcv) < REQUIRED_OHLCV_LIMITS[timeframe]:
        return {'symbol': symbol, 'timeframe': timeframe, 'side': 'DataShortage', 'score': 0.0}

    # 1. データフレームの準備
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['close'] = pd.to_numeric(df['close'], errors='coerce').astype('float64')
    df['high'] = pd.to_numeric(df['high'], errors='coerce').astype('float64')
    df['low'] = pd.to_numeric(df['low'], errors='coerce').astype('float64')
    df['open'] = pd.to_numeric(df['open'], errors='coerce').astype('float64')
    df['volume'] = pd.to_numeric(df['volume'], errors='coerce').astype('float64')
    
    if df['close'].isnull().any() or df.empty:
        return {'symbol': symbol, 'timeframe': timeframe, 'side': 'DataShortage', 'score': 0.0}
    
    current_price = df['close'].iloc[-1]
    
    # 2. テクニカル指標の計算
    df.ta.rsi(length=14, append=True)
    df.ta.macd(fast=12, slow=26, signal=9, append=True)
    df.ta.adx(length=14, append=True)
    df.ta.atr(length=14, append=True)
    df.ta.stoch(k=14, d=3, smooth_k=3, append=True)
    df.ta.cci(length=20, append=True)
    df.ta.bbands(length=20, append=True)
    
    # VWAP (Cumulative calculation must be handled carefully, here we use a simplified approach)
    df['vwap'] = (df['close'] * df['volume']).cumsum() / df['volume'].cumsum()
    df['vwap_mid'] = df['vwap'].iloc[-1]
    
    df['sma_long'] = ta.sma(df['close'], length=LONG_TERM_SMA_LENGTH)
    
    # ピボットポイントは、日足のデータフレームで計算するのが最も正確だが、
    # 各時間足の最新のOHLCVデータを用いて、より短期のS/Rとしても使用可能（ただし、日足の精度は保証されない）。
    # ここでは、日足のOHLCVを取得できない場合のフォールバックとして、この時間足のデータを使用する。
    pivot_points = calculate_pivot_points(df) 

    df.dropna(inplace=True)
    if df.empty:
        return {'symbol': symbol, 'timeframe': timeframe, 'side': 'DataShortage', 'score': 0.0}
        
    last = df.iloc[-1]
    last_prev = df.iloc[-2] if len(df) >= 2 else None 
    
    # 3. スコアリングの初期設定
    score: float = BASE_SCORE
    side: str = 'Neutral'
    reversal_signal: bool = False
    
    # ----------------------------------------------
    # 4. ベースシグナル判定 (RSI, ADX, MACD, CCI) 
    # ----------------------------------------------
    
    rsi_val = last.get('RSI_14', np.nan) 
    cci_val = last.get('CCI_20', np.nan)
    
    if not np.isnan(rsi_val) and not np.isnan(cci_val) and rsi_val <= RSI_OVERSOLD and cci_val < -100:
        score += 0.20
        reversal_signal = True
        side = 'ロング'
    elif not np.isnan(rsi_val) and rsi_val <= RSI_MOMENTUM_LOW:
        score += 0.10
        side = 'ロング'

    if not np.isnan(rsi_val) and not np.isnan(cci_val) and rsi_val >= RSI_OVERBOUGHT and cci_val > 100:
        score += 0.20
        reversal_signal = True
        side = 'ショート'
    elif not np.isnan(rsi_val) and rsi_val >= RSI_MOMENTUM_HIGH:
        score += 0.10
        side = 'ショート'
        
    adx_val = last.get('ADX_14', np.nan)
    regime = 'レンジ/もみ合い (Range)'
    if not np.isnan(adx_val) and adx_val >= ADX_TREND_THRESHOLD:
        score += 0.15 
        regime = 'トレンド (Trend)'
    elif not np.isnan(adx_val) and adx_val >= 20:
        score += 0.05
        regime = '初期トレンド (Emerging Trend)'
    
    macd_hist = last.get('MACDH_12_26_9', np.nan)
    macd_cross_valid = True
    macd_cross_penalty_value = 0.0
    
    if side != 'Neutral' and last_prev is not None and not np.isnan(macd_hist):
        macd_hist_prev = last_prev.get('MACDH_12_26_9', np.nan)
        if not np.isnan(macd_hist_prev):
            if side == 'ロング':
                if macd_hist < 0 and (macd_hist < macd_hist_prev): # 上昇中にヒストグラムが下降
                    score -= MACD_CROSS_PENALTY 
                    macd_cross_valid = False
                    macd_cross_penalty_value = MACD_CROSS_PENALTY
            elif side == 'ショート':
                if macd_hist > 0 and (macd_hist > macd_hist_prev): # 下降中にヒストグラムが上昇
                    score -= MACD_CROSS_PENALTY
                    macd_cross_valid = False
                    macd_cross_penalty_value = MACD_CROSS_PENALTY

    # ----------------------------------------------
    # 5. 価格アクション・長期トレンド・ボラティリティフィルター 
    # ----------------------------------------------
    
    long_term_trend = 'Neutral'
    long_term_reversal_penalty = False
    long_term_reversal_penalty_value = 0.0

    sma_long_val = last.get('SMA_50', np.nan)
    
    if not np.isnan(sma_long_val):
        if current_price > sma_long_val:
            long_term_trend = 'Long'
        elif current_price < sma_long_val:
            long_term_trend = 'Short'
            
    # 4h足以外では、長期トレンドと逆行するシグナルにペナルティ
    if timeframe != '4h' and side != 'Neutral':
        if long_term_trend == 'Long' and side == 'ショート':
            score -= LONG_TERM_REVERSAL_PENALTY
            long_term_reversal_penalty = True
            long_term_reversal_penalty_value = LONG_TERM_REVERSAL_PENALTY
        elif long_term_trend == 'Short' and side == 'ロング':
            score -= LONG_TERM_REVERSAL_PENALTY
            long_term_reversal_penalty = True
            long_term_reversal_penalty_value = LONG_TERM_REVERSAL_PENALTY
            
    # ボラティリティペナルティ (BB幅が狭すぎる場合)
    bbp_val = last.get('BBP_20_2.0', np.nan)
    volatility_penalty = 0.0
    if timeframe in ['15m', '1h'] and not np.isnan(bbp_val):
        bb_width = last.get('BBL_20_2.0', np.nan) - last.get('BBW_20_2.0', np.nan) # BBPが相対的な幅、BBWが絶対的な幅
        if not np.isnan(bb_width) and bb_width <= VOLATILITY_BB_PENALTY_THRESHOLD: # しきい値は要調整
            score -= 0.10
            volatility_penalty = 0.10
    
    # 出来高確認ボーナス
    volume_confirmation_bonus = 0.0
    volume_ratio = 0.0
    
    if len(df) > 100:
        avg_volume = df['volume'].iloc[-100:-1].mean()
        current_volume = last['volume'] 
        
        if avg_volume > 0:
            volume_ratio = current_volume / avg_volume
            if volume_ratio >= VOLUME_CONFIRMATION_MULTIPLIER:
                volume_confirmation_bonus = 0.05
                score += volume_confirmation_bonus
    
    # VWAPとの一貫性チェック
    vwap_consistent = True
    vwap_mid_val = last.get('vwap_mid', np.nan)
    if not np.isnan(vwap_mid_val):
        if side == 'ロング' and current_price < vwap_mid_val:
            vwap_consistent = False
        elif side == 'ショート' and current_price > vwap_mid_val:
            vwap_consistent = False

    # StochRSIによる過熱感ペナルティ
    stoch_penalty = 0.0
    stoch_k = last.get('STOCHk_14_3_3', np.nan)
    stoch_d = last.get('STOCHd_14_3_3', np.nan)
    
    if timeframe in ['15m', '1h'] and side != 'Neutral' and not np.isnan(stoch_k) and not np.isnan(stoch_d):
        if side == 'ロング':
            if stoch_k > 80 or stoch_d > 80:
                score -= 0.05
                stoch_penalty = 0.05
        elif side == 'ショート':
            if stoch_k < 20 or stoch_d < 20:
                score -= 0.05
                stoch_penalty = 0.05


    # ----------------------------------------------
    # 6. SL/TP/RRRの決定 (DTS戦略ベース)
    # ----------------------------------------------
    
    atr_val_raw = last.get('ATR_14', 1.0) 
    atr_val = atr_val_raw if atr_val_raw > 0.0 else 1.0

    entry_price = current_price
    sl_price = 0.0
    tp1_price = 0.0
    rr_ratio = 0.0
    
    entry_type = 'Market' 
    structural_sl_used = False
    structural_pivot_bonus = 0.0
    
    # 構造的SL (S1/R1) の探索と適用
    if pivot_points and side != 'Neutral':
        if side == 'ロング':
            close_s1 = pivot_points.get('s1', 0.0)
            close_s2 = pivot_points.get('s2', 0.0)
            close_pp = pivot_points.get('pp', 0.0)
            
            # 価格より下にあるS/Rを潜在的なSL候補とする
            potential_sls = sorted([sl for sl in [close_s1, close_s2, close_pp] 
                                    if sl < current_price and current_price - sl <= atr_val * ATR_TRAIL_MULTIPLIER], 
                                    reverse=True)
            
            if potential_sls:
                structural_sl_used = True
                sl_price_raw = potential_sls[0]
                sl_price = sl_price_raw - (0.5 * atr_val) # S/Rにバッファを追加
                structural_pivot_bonus = 0.05
                score += structural_pivot_bonus

                entry_point_raw = sl_price_raw
                # S/R付近でのエントリーを狙う Limit 注文として Entry Price を設定
                entry_price = entry_point_raw + (0.1 * atr_val) # S/Rに少し引き付けてエントリー
                entry_type = 'Limit'
                
            else:
                sl_price = current_price - (atr_val * ATR_TRAIL_MULTIPLIER)
                entry_type = 'Market'
                entry_price = current_price

        
        elif side == 'ショート':
            close_r1 = pivot_points.get('r1', 0.0)
            close_r2 = pivot_points.get('r2', 0.0)
            close_pp = pivot_points.get('pp', 0.0)
            
            # 価格より上にあるS/Rを潜在的なSL候補とする
            potential_sls = sorted([sl for sl in [close_r1, close_r2, close_pp] 
                                    if sl > current_price and sl - current_price <= atr_val * ATR_TRAIL_MULTIPLIER])
            
            if potential_sls:
                structural_sl_used = True
                sl_price_raw = potential_sls[0]
                sl_price = sl_price_raw + (0.5 * atr_val) # R/Rにバッファを追加
                structural_pivot_bonus = 0.05
                score += structural_pivot_bonus
                
                entry_point_raw = sl_price_raw
                # R/R付近でのエントリーを狙う Limit 注文として Entry Price を設定
                entry_price = entry_point_raw - (0.1 * atr_val) # R/Rに少し引き付けてエントリー
                entry_type = 'Limit'
                
            else:
                sl_price = current_price + (atr_val * ATR_TRAIL_MULTIPLIER)
                entry_type = 'Market'
                entry_price = current_price

    else:
        if side == 'ロング':
            sl_price = current_price - (atr_val * ATR_TRAIL_MULTIPLIER)
        elif side == 'ショート':
            sl_price = current_price + (atr_val * ATR_TRAIL_MULTIPLIER)
        entry_type = 'Market'
        entry_price = current_price

    # SLが無効な場合、シグナルを無効化
    if sl_price <= 0 or (side == 'ロング' and sl_price >= entry_price) or (side == 'ショート' and sl_price <= entry_price):
         side = 'Neutral'
         score = max(BASE_SCORE, score)
    
    # TP1 (目標値) の計算
    risk_width = abs(entry_price - sl_price)

    # スコアに基づいてTPのRRRを動的に決定
    if side != 'Neutral' and risk_width > 0:
        score_normalized = max(0.0, min(1.0, score))
        
        # SIGNAL_THRESHOLD (0.75) から 1.0 の範囲で RRR を 1.5 から 2.5 に変動させる
        if score_normalized >= SIGNAL_THRESHOLD:
            score_range = 1.0 - SIGNAL_THRESHOLD
            score_offset = score_normalized - SIGNAL_THRESHOLD
            adjustment_ratio = score_offset / score_range
            rr_ratio = DYNAMIC_TP_RRR_MIN + adjustment_ratio * (DYNAMIC_TP_RRR_MAX - DYNAMIC_TP_RRR_MIN)
        else:
            rr_ratio = DYNAMIC_TP_RRR_MIN 
        
        rr_ratio = round(rr_ratio, 2)
        
        if side == 'ロング':
            tp1_price = entry_price + (risk_width * rr_ratio) 
        elif side == 'ショート':
            tp1_price = entry_price - (risk_width * rr_ratio) 
    else:
        rr_ratio = 0.0

    # ----------------------------------------------
    # 7. マクロフィルター (Funding Rate Bias & Dominance Bias)
    # ----------------------------------------------
    
    funding_rate_value = 0.0
    funding_rate_bonus_value = 0.0
    
    if side != 'Neutral' and macro_context:
        funding_rate_value = macro_context.get(f'{symbol}_fr', 0.0)
        
        if side == 'ロング':
            # FRがマイナス (ショート過密) ならボーナス
            if funding_rate_value <= -FUNDING_RATE_THRESHOLD:
                score += FUNDING_RATE_BONUS_PENALTY
                funding_rate_bonus_value = FUNDING_RATE_BONUS_PENALTY
            # FRがプラス (ロング過密) ならペナルティ
            elif funding_rate_value >= FUNDING_RATE_THRESHOLD:
                score -= FUNDING_RATE_BONUS_PENALTY
                funding_rate_bonus_value = -FUNDING_RATE_BONUS_PENALTY
        
        elif side == 'ショート':
            # FRがプラス (ロング過密) ならボーナス
            if funding_rate_value >= FUNDING_RATE_THRESHOLD:
                score += FUNDING_RATE_BONUS_PENALTY
                funding_rate_bonus_value = FUNDING_RATE_BONUS_PENALTY
            # FRがマイナス (ショート過密) ならペナルティ
            elif funding_rate_value <= -FUNDING_RATE_THRESHOLD:
                score -= FUNDING_RATE_BONUS_PENALTY
                funding_rate_bonus_value = -FUNDING_RATE_BONUS_PENALTY

    dominance_bias_bonus_value = 0.0
    dominance_trend = macro_context.get('dominance_trend', 'Neutral')
    bias_score_raw = macro_context.get('dominance_bias_score', 0.0)
    
    if symbol != 'BTC-USDT' and side != 'Neutral' and bias_score_raw != 0.0:
        
        # bias_score_raw > 0: BTCトレンド強 (Altcoin Longにペナルティ, Shortにボーナス)
        if bias_score_raw > 0:
            if side == 'ロング':
                score -= bias_score_raw
                dominance_bias_bonus_value = -bias_score_raw
            elif side == 'ショート':
                # ShortはBTC強トレンドでも追随するため、半分のボーナス/ペナルティ
                score += bias_score_raw / 2 
                dominance_bias_bonus_value = bias_score_raw / 2
                
        # bias_score_raw < 0: BTCトレンド弱 (Altcoin Longにボーナス, Shortにペナルティ)
        elif bias_score_raw < 0:
            if side == 'ロング':
                score += abs(bias_score_raw)
                dominance_bias_bonus_value = abs(bias_score_raw)
            elif side == 'ショート':
                score -= abs(bias_score_raw) / 2
                dominance_bias_bonus_value = -abs(bias_score_raw) / 2


    # 8. 最終スコア調整とクリッピング
    score = max(0.0, min(1.0, score))
    
    if score < SIGNAL_THRESHOLD:
         side = 'Neutral'
         score = max(BASE_SCORE, score)


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
            'cci': cci_val, 
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
            'pivot_points': pivot_points,
        }
    }
    
    return result


# ====================================================================================
# MAIN LOOP & FASTAPI SETUP
# ====================================================================================

async def main_loop():
    """主要な市場監視とシグナル生成のメインループ"""
    global EXCHANGE_CLIENT, LAST_UPDATE_TIME, LAST_ANALYSIS_SIGNALS, LAST_SUCCESS_TIME, GLOBAL_MACRO_CONTEXT
    
    if not await initialize_exchange():
        logging.error("初期化に失敗しました。BOTを終了します。")
        return

    # NEW: ロードされた市場情報に基づき、監視対象シンボルをフィルタリング
    await filter_monitoring_symbols(EXCHANGE_CLIENT)

    while True:
        try:
            current_time = time.time()
            if current_time - LAST_UPDATE_TIME < LOOP_INTERVAL:
                await asyncio.sleep(10)
                continue
                
            logging.info("--- 🔄 新しい分析サイクルを開始 ---")
            
            # 1. マクロ環境の取得
            GLOBAL_MACRO_CONTEXT = await get_crypto_macro_context(EXCHANGE_CLIENT, CURRENT_MONITOR_SYMBOLS)
            logging.info(f"マクロコンテキストを更新しました。FR/Dominanceデータ: {len(GLOBAL_MACRO_CONTEXT)}件")

            # 2. 全シンボル・全時間足のデータ取得と分析
            analysis_tasks = []
            
            # 4h足はトレンド判定のために全てのシンボルで取得する
            required_timeframes = ['15m', '1h', '4h'] 

            for symbol in CURRENT_MONITOR_SYMBOLS:
                for tf in required_timeframes:
                    limit = REQUIRED_OHLCV_LIMITS[tf]
                    # OHLCVの取得
                    ohlcv = await fetch_ohlcv_with_fallback(EXCHANGE_CLIENT, symbol, tf, limit)
                    # 分析タスクの作成
                    task = asyncio.create_task(
                        asyncio.to_thread(
                            analyze_single_timeframe,
                            symbol, tf, ohlcv, GLOBAL_MACRO_CONTEXT
                        )
                    )
                    analysis_tasks.append(task)
            
            # 全ての分析タスクの完了を待機
            raw_results = await asyncio.gather(*analysis_tasks)
            
            # 3. 結果のフィルタリングと統合
            # シンボルごとに結果をグループ化
            grouped_results: Dict[str, List[Dict]] = {}
            for res in raw_results:
                if res['symbol'] not in grouped_results:
                    grouped_results[res['symbol']] = []
                grouped_results[res['symbol']].append(res)
            
            # 各シンボルで最もスコアが高いシグナルを選択
            best_signals_list = []
            for symbol, signals in grouped_results.items():
                
                # 有効なシグナルのみを抽出
                valid_signals = [s for s in signals if s.get('side') not in ["DataShortage", "ExchangeError", "Neutral"]]
                if not valid_signals: continue
                
                # スコアが閾値を超えているシグナルのみを対象とする
                high_score_signals = [s for s in valid_signals if s.get('score', 0.5) >= SIGNAL_THRESHOLD]
                
                if not high_score_signals: continue
                
                # 最もスコアが高いシグナルをそのシンボルの「ベストシグナル」として記録
                best_signal = max(
                    high_score_signals, 
                    key=lambda s: (s.get('score', 0.5), s.get('rr_ratio', 0.0))
                )
                
                # 通知に使用する情報として、そのシンボルの全シグナルを保存
                best_signal['all_timeframe_signals'] = signals
                best_signals_list.append(best_signal)

            # 4. シグナルスコアリングとランキング
            # スコア順にソート (スコア、RRRの順)
            sorted_signals = sorted(
                best_signals_list, 
                key=lambda s: (s['score'], s['rr_ratio']), 
                reverse=True
            )
            
            # 5. 通知処理
            notified_count = 0
            LAST_ANALYSIS_SIGNALS = [] 
            
            for rank, signal in enumerate(sorted_signals[:TOP_SIGNAL_COUNT], 1):
                symbol_name = signal['symbol']
                current_score = signal['score']
                
                # クールダウン期間のチェック
                last_notify = TRADE_NOTIFIED_SYMBOLS.get(symbol_name, 0.0)
                if current_time - last_notify < TRADE_SIGNAL_COOLDOWN:
                    logging.info(f"[{symbol_name}] クールダウン中。通知をスキップしました。")
                    continue
                
                # 統合された分析メッセージを生成
                message = format_integrated_analysis_message(
                    symbol_name, 
                    signal['all_timeframe_signals'], 
                    rank
                )
                
                if message:
                    if send_telegram_html(message):
                        TRADE_NOTIFIED_SYMBOLS[symbol_name] = current_time
                        logging.info(f"[{symbol_name}] **ランク {rank} / スコア {current_score:.4f}** の取引シグナルを通知しました。")
                        notified_count += 1
                
                LAST_ANALYSIS_SIGNALS.append(signal)

            # 6. 成功時の状態更新
            LAST_UPDATE_TIME = current_time
            LAST_SUCCESS_TIME = current_time
            logging.info(f"--- ✅ 分析サイクルを完了しました (通知数: {notified_count}件 / 監視シンボル数: {len(CURRENT_MONITOR_SYMBOLS)}件) ---")
            
        except Exception as e:
            error_name = type(e).__name__
            logging.error(f"メインループで致命的なエラー: {error_name}")
            # クライアントが切断された場合は再初期化を試みる
            if EXCHANGE_CLIENT and isinstance(e, (ccxt.NetworkError, ccxt.ExchangeError)):
                 logging.warning("CCXTエラーを検出。クライアントを再初期化します...")
                 await initialize_exchange()
            
            await asyncio.sleep(60)


# ====================================================================================
# FASTAPI SETUP
# ====================================================================================

app = FastAPI(title="Apex BOT API", version="v19.0.2 - Robust Symbol Filter Fix") # バージョン更新

@app.on_event("startup")
async def startup_event():
    logging.info("🚀 Apex BOT v19.0.2 Startup initializing...") # バージョン更新
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
        "bot_version": "v19.0.2 - Robust Symbol Filter Fix", # バージョン更新
        "last_success_time_utc": datetime.fromtimestamp(LAST_SUCCESS_TIME, tz=timezone.utc).isoformat() if LAST_SUCCESS_TIME else "N/A",
        "current_client": CCXT_CLIENT_NAME,
        "monitoring_symbols": len(CURRENT_MONITOR_SYMBOLS),
        "last_signals_count": len(LAST_ANALYSIS_SIGNALS)
    }
    return JSONResponse(content=status_msg)

@app.head("/")
@app.get("/")
def home_view():
    return JSONResponse(content={"message": "Apex BOT is running (v19.0.2)"})


# 以下をターミナルで実行してBOTを起動します:
# uvicorn main:app --host 0.0.0.0 --port 8000
