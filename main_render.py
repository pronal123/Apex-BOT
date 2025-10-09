# ====================================================================================
# Apex BOT v21.0.0 - PnL Projection Edition (完全版)
# - NEW: 想定取引サイズに基づき、TP/SL到達時の想定損益額を計算・表示
# - NEW: 時間軸 (Timeframe) に応じてTP目標となるRRRを動的に変更 (v20.0.0)
# - NEW: Ichimoku Kinko Hyo, True Strength Index (TSI) の分析統合 (v19.0.0)
# - MOD: Telegram通知のハイパー・ビジュアル化 (v19.0.0)
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
import yfinance as yf # BTCドミナンス取得用
import asyncio
from fastapi import FastAPI
from fastapi.responses import JSONResponse 
import uvicorn
from dotenv import load_dotenv
import sys 
import random 
import re 

# .envファイルから環境変数を読み込む
load_dotenv()

# ====================================================================================
# CONFIG & CONSTANTS (v21.0.0)
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

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', 'YOUR_TELEGRAM_CHAT_ID')

# NEW: 損益計算の基準となる想定取引サイズ (USDT建て) - 必要に応じてこの値を変更してください
TRADE_SIZE_USDT = 5000.0  

TRADE_SIGNAL_COOLDOWN = 60 * 60 * 2 
SIGNAL_THRESHOLD = 0.77             
TOP_SIGNAL_COUNT = 3                
REQUIRED_OHLCV_LIMITS = {'15m': 500, '1h': 500, '4h': 500} 
VOLATILITY_BB_PENALTY_THRESHOLD = 5.0 

LONG_TERM_REVERSAL_PENALTY = 0.20   
MACD_CROSS_PENALTY = 0.15           

# Dynamic Trailing Stop (DTS) Parameters
ATR_TRAIL_MULTIPLIER = 3.0          
# Adaptive RRR based on Timeframe (v20.0.0)
TIMEframe_RRR_MAP = {
    '15m': 3.5, 
    '1h': 5.0,  
    '4h': 7.0   
}

# Dynamic Entry Optimization (DEO) Parameters
ATR_ENTRY_PULLBACK_MULTIPLIER = 0.6 

# Funding Rate Bias Filter Parameters
FUNDING_RATE_THRESHOLD = 0.00015    
FUNDING_RATE_BONUS_PENALTY = 0.08   

# Dominance Bias Filter Parameters
DOMINANCE_BIAS_SCORE = 0.05 # ドミナンス増加/減少がトレンドと一致した場合のスコア
DOMINANCE_BIAS_PENALTY = 0.05 # ドミナンス増加/減少がトレンドと逆行した場合のペナルティ

# スコアリングロジック用の定数 (v19.0.0から維持)
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
RSI_MOMENTUM_LOW = 40               
RSI_MOMENTUM_HIGH = 60              
ADX_TREND_THRESHOLD = 30            
BASE_SCORE = 0.40                   
VOLUME_CONFIRMATION_MULTIPLIER = 2.5 

# MTF Trend Convergence (TSC) Parameters
MTF_CONVERGENCE_BONUS = 0.10 

# Ichimoku/TSI スコアリング (v19.0.0から維持)
ICHIMOKU_BONUS = 0.10 
TSI_BONUS = 0.05
TSI_OVERSOLD = -30 
TSI_OVERBOUGHT = 30 

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
# CORE CCXT & DATA UTILITIES
# ====================================================================================

async def initialize_client(client_name: str) -> Optional[ccxt_async.Exchange]:
    """CCXTクライアントを初期化する"""
    global EXCHANGE_CLIENT
    
    config = {
        'enableRateLimit': True,
        'apiKey': os.environ.get('EXCHANGE_API_KEY'),
        'secret': os.environ.get('EXCHANGE_SECRET'),
        'options': {'defaultType': 'future'},
        'timeout': 15000
    }
    
    if client_name in ccxt.exchanges:
        client_class = getattr(ccxt_async, client_name.lower())
        EXCHANGE_CLIENT = client_class(config)
        logging.info(f"CCXTクライアント '{client_name}' を初期化しました。")
        return EXCHANGE_CLIENT
    else:
        logging.error(f"指定された取引所 '{client_name}' はCCXTでサポートされていません。")
        return None

async def fetch_ohlcv_with_fallback(client_name: str, symbol: str, timeframe: str) -> Tuple[List[List], str, str]:
    """OHLCVデータを取得し、エラー発生時はフォールバック処理を行う"""
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT:
        await initialize_client(client_name)
    
    max_retries = 3
    ohlcv = []
    
    for attempt in range(max_retries):
        try:
            if not EXCHANGE_CLIENT:
                await initialize_client(client_name)
                if not EXCHANGE_CLIENT:
                    return [], "ExchangeError", client_name
            
            # CCXTのシンボル形式に変換
            ccxt_symbol = symbol.replace('-', '/')
            
            # 必要なOHLCV制限をチェック
            limit = REQUIRED_OHLCV_LIMITS.get(timeframe, 500)
            
            # OHLCVデータを取得
            ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(ccxt_symbol, timeframe, limit=limit)
            
            if not ohlcv:
                logging.warning(f"⚠️ {symbol} [{timeframe}] のデータが空でした。")
                return [], "DataShortage", client_name
            
            # 取得データのクリーンアップ (最後の未確定バーを除外)
            # 現在時刻がバーの終わりに近い場合、最新のバーを使うこともあるため、ここではシンプルに最新の確定済みバーを想定
            if len(ohlcv) > 1:
                ohlcv = ohlcv[:-1] 
            
            if len(ohlcv) < limit * 0.9: # 9割未満ならデータ不足と判断
                return ohlcv, "DataShortage", client_name
                
            return ohlcv, "Success", client_name
        
        except ccxt.NetworkError as e:
            logging.warning(f"ネットワークエラー ({attempt + 1}/{max_retries}): {e}")
            await asyncio.sleep(2)
        except Exception as e:
            error_name = type(e).__name__
            logging.error(f"CCXTエラー ({symbol} [{timeframe}]): {error_name}: {e}")
            return [], "ExchangeError", client_name
            
    return [], "ExchangeError", client_name

async def fetch_funding_rate(symbol: str) -> float:
    """現在の資金調達率を取得する"""
    if not EXCHANGE_CLIENT:
        return 0.0
        
    try:
        ccxt_symbol = symbol.replace('-', '/')
        # CCXTには統一されたファンディングレートの取得メソッドがないため、fetch_tickerのinfoを使うか、fetch_funding_rateを直接呼ぶ
        # OKXの場合、fetch_funding_rateが利用可能
        if hasattr(EXCHANGE_CLIENT, 'fetch_funding_rate'):
            funding_rate_data = await EXCHANGE_CLIENT.fetch_funding_rate(ccxt_symbol)
            return funding_rate_data.get('fundingRate', 0.0)
        else:
             ticker = await EXCHANGE_CLIENT.fetch_ticker(ccxt_symbol)
             return ticker.get('info', {}).get('interestRate', 0.0) # 例: OKXのinfoから取得
             
    except Exception as e:
        # logging.warning(f"資金調達率の取得エラー: {e}")
        return 0.0


# ====================================================================================
# UTILITIES & FORMATTING (v21.0.0 PnL表示対応)
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

def format_pnl_utility(amount: float) -> str:
    """想定損益額の表示を整形 (USD、小数点以下2桁)"""
    if amount is None: return "$0.00"
    return f"${amount:,.2f}"

def send_telegram_html(message: str) -> bool:
    """TelegramにHTML形式でメッセージを送信する (変更なし)"""
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
    """スコアと時間軸に基づき推定勝率を算出する (変更なし)"""
    adjusted_rate = 0.50 + (score - 0.50) * 1.55 
    return max(0.40, min(0.90, adjusted_rate))


def format_integrated_analysis_message(symbol: str, signals: List[Dict], rank: int) -> str:
    """
    3つの時間軸の分析結果を統合し、ログメッセージの形式に整形する (v21.0.0 - 想定損益額を追加)
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
    tp_price = best_signal.get('tp1', 0.0) 
    sl_price = best_signal.get('sl', 0.0) 
    entry_type = best_signal.get('entry_type', 'N/A') 

    score_100 = score_raw * 100
    win_rate = get_estimated_win_rate(score_raw, timeframe) * 100
    display_symbol = symbol.replace('-', '/')
    
    sl_width = abs(entry_price - sl_price)
    
    # ----------------------------------------------------
    # NEW: 想定損益額の計算 (v21.0.0)
    # ----------------------------------------------------
    estimated_profit = 0.0
    estimated_loss = 0.0
    
    if entry_price > 0 and sl_width > 0:
        # 想定ポジション量 (Quantity) を計算: TRADE_SIZE_USDTをエントリー価格で割る
        quantity = TRADE_SIZE_USDT / entry_price
        
        # 想定利益額
        estimated_profit = quantity * abs(tp_price - entry_price)
        # 想定損失額 (Risk Amount)
        estimated_loss = quantity * sl_width
    
    # ----------------------------------------------------
    # 1. ヘッダーと主要因のハイライト (v19.0.0/v20.0.0から維持)
    # ----------------------------------------------------
    direction_emoji = "🔼 **ロング (LONG)**" if side == "ロング" else "🔽 **ショート (SHORT)**"
    strength = "🔥 極めて強力 (ULTRA HIGH)" if score_raw >= 0.85 else ("📈 高確度 (HIGH)" if score_raw >= 0.77 else "🟡 中 (MEDIUM)")
    
    rank_header = ""
    if rank == 1: rank_header = "👑 **総合 <ins>1 位</ins>！**"
    elif rank == 2: rank_header = "🥈 **総合 2 位！**"
    elif rank == 3: rank_header = "🥉 **総合 3 位！**"
    else: rank_header = f"🏆 **総合 {rank} 位！**"

    tech_data = best_signal.get('tech_data', {})
    
    main_reason = "💡 高確度モメンタム加速"
    if tech_data.get('structural_pivot_bonus', 0.0) > 0.0 and entry_type == "Limit":
        main_reason = "🎯 構造的S/Rからの **Limit** エントリー"
    elif tech_data.get('ichimoku_bonus', 0.0) >= ICHIMOKU_BONUS:
        main_reason = "⛩️ 一目均衡表 **Kumoブレイク** / 大転換"
    elif tech_data.get('mtf_convergence_bonus', 0.0) > 0.0:
        main_reason = "✨ **MTFトレンド収束**による極大優位性"
    elif tech_data.get('volume_confirmation_bonus', 0.0) > 0.0:
        main_reason = "⚡ 出来高/流動性を伴う **ブレイクアウト**"

    header = (
        f"🚨 **{display_symbol}** **{direction_emoji}** シグナル発生！\n"
        f"==================================\n"
        f"| {rank_header} | {strength} |\n"
        f"| 🎯 **予測勝率** | **<ins>{win_rate:.1f}%</ins>** |\n"
        f"| 💯 **分析スコア** | <b>{score_100:.2f} / 100.00 点</b> (ベース: {timeframe}足) |\n" 
        f"==================================\n"
        f"**📣 シグナル主要因**: **{main_reason}**\n"
        f"==================================\n"
    )

    sl_source_str = "ATR基準"
    if best_signal.get('tech_data', {}).get('structural_sl_used', False):
        sl_source_str = "構造的 (Pivot) + **0.5 ATR バッファ**" 
        
    entry_optimize_text = ""
    if entry_type == "Limit":
        entry_optimize_text = f" (DEO: ATR x {ATR_ENTRY_PULLBACK_MULTIPLIER:.1f} PULLBACK)"
    
    # 2. 取引計画の表示 (v21.0.0 - 損益額の行を追加)
    trade_plan = (
        f"**📊 取引計画 (DTS & Structural SL)**\n"
        f"----------------------------------\n"
        f"| 指標 | 価格 (USD) | 備考 |\n"
        f"| :--- | :--- | :--- |\n"
        f"| 💰 現在価格 | <code>${format_price_utility(price, symbol)}</code> | 参照 |\n"
        f"| ➡️ **Entry ({entry_type})** | <code>${format_price_utility(entry_price, symbol)}</code> | **{side}** (**<ins>推奨</ins>**){entry_optimize_text} |\n" 
        f"| 📉 **Risk (SL幅)** | ${format_price_utility(sl_width, symbol)} | **初動リスク** (ATR x {ATR_TRAIL_MULTIPLIER:.1f}) |\n"
        f"| 🟢 TP 目標 | <code>${format_price_utility(tp_price, symbol)}</code> | **動的決済** (適応型TP: RRR 1:{rr_ratio:.2f}) |\n" 
        f"| ❌ SL 位置 | <code>${format_price_utility(sl_price, symbol)}</code> | 損切 ({sl_source_str}) |\n"
        f"----------------------------------\n"
        f"**💰 想定損益額 (取引サイズ: <ins>{format_pnl_utility(TRADE_SIZE_USDT)}</ins> 相当)**\n"
        f"----------------------------------\n"
        f"| 📈 **想定利益** | <b>{format_pnl_utility(estimated_profit)}</b> | **+ {estimated_profit/TRADE_SIZE_USDT*100:.2f}%** (目安) |\n"
        f"| 🔻 **想定損失** | <b>{format_pnl_utility(estimated_loss)}</b> | **- {estimated_loss/TRADE_SIZE_USDT*100:.2f}%** (最大リスク) |\n"
        f"----------------------------------\n"
    )
    
    # 3. 統合分析サマリーの詳細 (v19.0.0から維持)
    analysis_detail = "**🔬 統合分析サマリー (3時間軸)**\n"
    
    mtf_bonus = tech_data.get('mtf_convergence_bonus', 0.0)
    if mtf_bonus > 0:
        analysis_detail += f"✨ **MTFトレンド収束**: 3時間軸トレンド一致！ **+{mtf_bonus * 100:.2f}点** ボーナス！\n"
    
    long_term_trend_4h = 'Neutral'
    
    for s in signals:
        tf = s.get('timeframe')
        s_side = s.get('side', 'N/A')
        s_score = s.get('score', 0.5)
        tech_data_s = s.get('tech_data', {})
        
        score_in_100 = s_score * 100
        
        if tf == '4h':
            long_term_trend_4h = tech_data_s.get('long_term_trend', 'Neutral')
            analysis_detail += (
                f"🌏 **4h 足** (長期トレンド): **{long_term_trend_4h}** ({score_in_100:.2f}点)\n"
            )
            
        else:
            score_icon = "🔥" if s_score >= 0.77 else ("📈" if s_score >= 0.65 else "🟡" )
            
            penalty_status = f" (逆張りペナルティ: -{tech_data_s.get('long_term_reversal_penalty_value', 0.0) * 100:.1f}点適用)" if tech_data_s.get('long_term_reversal_penalty') else ""
            
            momentum_valid = tech_data_s.get('macd_cross_valid', True)
            momentum_text = "[✅ モメンタム確証: OK]" if momentum_valid else f"[⚠️ モメンタム反転により減点: -{tech_data_s.get('macd_cross_penalty_value', 0.0) * 100:.1f}点]"

            vwap_consistent = tech_data_s.get('vwap_consistent', False)
            vwap_text = "[🌊 VWAP一致: OK]" if vwap_consistent else "[🌊 VWAP不一致: NG]"
            
            # Ichimoku/TSI Status
            ichimoku_conf = tech_data_s.get('ichimoku_confirmation', 'Neutral')
            tsi_conf = tech_data_s.get('tsi_confirmation', 'Neutral')
            
            ichimoku_text = "⛩️ Ichimoku OK" if ichimoku_conf == s_side else "❌ Ichimoku NG"
            tsi_text = "🚀 TSI OK" if tsi_conf == s_side else "❌ TSI NG"


            analysis_detail += (
                f"**[{tf} 足] {score_icon}** ({score_in_100:.2f}点) -> **{s_side}**{penalty_status}\n"
            )
            analysis_detail += f"   └ {momentum_text} {vwap_text}\n"
            
            # 採用された時間軸の技術指標を詳細に表示
            if tf == timeframe:
                regime = best_signal.get('regime', 'N/A')
                # ADX/Regime
                analysis_detail += f"   └ **トレンド指標**: ADX:{tech_data.get('adx', 0.0):.2f} ({regime}), {ichimoku_text}\n"
                analysis_detail += f"   └ **モメンタム指標**: RSI:{tech_data.get('rsi', 0.0):.1f}, TSI:{tech_data.get('tsi', 0.0):.1f} ({tsi_text})\n"
                
                # Volatility Adaptive Filter (VAF) の表示
                vaf_penalty = tech_data.get('vaf_penalty_value', 0.0)
                vaf_text = ""
                if vaf_penalty > 0:
                    vaf_text = f" [⚠️ 低ボラティリティにより減点: -{vaf_penalty * 100:.2f}点]"
                elif regime == "レンジ":
                    vaf_text = " [✅ レンジ相場適応]"
                analysis_detail += f"   └ **VAF (ボラティリティ)**: BB幅{tech_data.get('bb_width_pct', 0.0):.2f}% {vaf_text}\n"

                # Structural/Pivot Analysis
                pivot_bonus = tech_data.get('structural_pivot_bonus', 0.0)
                pivot_status = "✅ 構造的S/R確証" if pivot_bonus > 0 else "❌ 構造確証なし"
                analysis_detail += f"   └ **構造分析(Pivot)**: {pivot_status} (+{pivot_bonus * 100:.2f}点)\n"

                # 出来高確証の表示
                volume_bonus = tech_data.get('volume_confirmation_bonus', 0.0)
                if volume_bonus > 0:
                    analysis_detail += f"   └ **出来高/流動性確証**: ✅ +{volume_bonus * 100:.2f}点 (平均比率: {tech_data.get('volume_ratio', 0.0):.1f}x)\n"
                
                # Funding Rate Analysis
                funding_rate_val = tech_data.get('funding_rate_value', 0.0)
                funding_rate_bonus = tech_data.get('funding_rate_bonus_value', 0.0)
                funding_rate_status = ""
                if funding_rate_bonus > 0:
                    funding_rate_status = f"✅ 優位性あり (+{funding_rate_bonus * 100:.2f}点)"
                elif funding_rate_bonus < 0:
                    funding_rate_status = f"⚠️ 過密ペナルティ (-{abs(funding_rate_bonus) * 100:.2f}点)"
                else:
                    funding_rate_status = "❌ フィルター範囲外"
                
                analysis_detail += f"   └ **資金調達率 (FR)**: {funding_rate_val * 100:.4f}% - {funding_rate_status}\n"

    # 4. リスク管理とフッター
    regime = best_signal.get('regime', 'N/A')
    
    footer = (
        f"==================================\n"
        f"| 🕰️ **TP到達目安** | **{get_tp_reach_time(timeframe)}** |\n"
        f"| 🔍 **市場環境** | **{regime}** 相場 (ADX: {best_signal.get('tech_data', {}).get('adx', 0.0):.2f}) |\n"
        f"| ⚙️ **BOT Ver** | **v21.0.0** - PnL Projection Edition |\n" 
        f"==================================\n"
        f"\n<pre>※ Limit注文は指定水準到達時のみ約定します。DTSはSLを自動追跡し利益を最大化します。</pre>"
    )

    return header + trade_plan + analysis_detail + footer


# ====================================================================================
# CORE ANALYSIS LOGIC
# ====================================================================================

def calculate_fib_pivot(df: pd.DataFrame) -> Dict[str, float]:
    """
    直近の確定した日の高値・安値・終値に基づき、クラシックなフィボナッチピボットレベルを計算する。
    """
    if df.empty or len(df) < 1:
        return {"P": 0.0, "R1": 0.0, "S1": 0.0, "R2": 0.0, "S2": 0.0}

    # 最新の確定したバーを使用
    last_bar = df.iloc[-1]
    H = last_bar['high']
    L = last_bar['low']
    C = last_bar['close']
    
    # クラシックピボット
    P = (H + L + C) / 3
    
    # サポートとレジスタンス
    R1 = 2 * P - L
    S1 = 2 * P - H
    R2 = P + (H - L)
    S2 = P - (H - L)

    # 構造的サポート/レジスタンスとしてR1/S1を使用
    return {"P": P, "R1": R1, "S1": S1, "R2": R2, "S2": S2}

def analyze_structural_proximity(price: float, pivots: Dict[str, float], side: str) -> Tuple[float, float, float]:
    """
    現在の価格がFibonacci PivotのR1/S1に近接しているかを確認し、ボーナスと構造的TP/SLを提供する。
    """
    structural_pivot_bonus = 0.0
    structural_sl_pivot = 0.0
    structural_tp_pivot = 0.0

    R1 = pivots.get("R1", 0.0)
    S1 = pivots.get("S1", 0.0)

    # 構造的S/Rに近接しているかを確認 (ここではR1/S1を使用)
    if side == "ロング" and S1 > 0:
        # 価格がS1の上にあり、S1がSLとして機能する可能性
        if price > S1 and abs(price - S1) / price < 0.01: # 1%以内
            structural_pivot_bonus = 0.07 
            structural_sl_pivot = S1
            structural_tp_pivot = R1

    elif side == "ショート" and R1 > 0:
        # 価格がR1の下にあり、R1がSLとして機能する可能性
        if price < R1 and abs(price - R1) / price < 0.01: # 1%以内
            structural_pivot_bonus = 0.07 
            structural_sl_pivot = R1
            structural_tp_pivot = S1

    return structural_pivot_bonus, structural_sl_pivot, structural_tp_pivot


async def analyze_single_timeframe(symbol: str, timeframe: str, macro_context: Dict, client_name: str, long_term_trend: str, mtf_convergence_trend: str, mtf_convergence_applied: bool) -> Optional[Dict]:
    """
    単一の時間軸で分析とシグナル生成を行う関数 (v20.0.0 - 適応型TP適用)
    """
    
    # 1. データ取得とFunding Rate取得
    ohlcv, status, client_used = await fetch_ohlcv_with_fallback(client_name, symbol, timeframe)
    
    funding_rate_val = 0.0
    if timeframe == '1h': 
        funding_rate_val = await fetch_funding_rate(symbol)
    
    
    tech_data_defaults = {
        "rsi": 50.0, "macd_hist": 0.0, "adx": 25.0, "bb_width_pct": 0.0, "atr_value": 0.005,
        "long_term_trend": long_term_trend, "long_term_reversal_penalty": False, "macd_cross_valid": False,
        "cci": 0.0, "vwap_consistent": False, "ppo_hist": 0.0, "dc_high": 0.0, "dc_low": 0.0,
        "stoch_k": 50.0, "stoch_d": 50.0, "stoch_filter_penalty": 0.0,
        "volume_confirmation_bonus": 0.0, "current_volume": 0.0, "average_volume": 0.0,
        "sentiment_fgi_proxy_bonus": 0.0, "structural_pivot_bonus": 0.0, 
        "volume_ratio": 0.0, "structural_sl_used": False,
        "long_term_reversal_penalty_value": 0.0, 
        "macd_cross_penalty_value": 0.0,
        "funding_rate_value": funding_rate_val, 
        "funding_rate_bonus_value": 0.0, 
        "dynamic_exit_strategy": "DTS",
        "dominance_trend": "Neutral",
        "dominance_bias_bonus_value": 0.0,
        "vaf_penalty_value": 0.0, 
        "mtf_convergence_bonus": MTF_CONVERGENCE_BONUS if mtf_convergence_applied else 0.0,
        "ichimoku_confirmation": "Neutral",
        "ichimoku_bonus": 0.0,
        "tsi": 0.0,
        "tsi_confirmation": "Neutral",
        "tsi_bonus": 0.0,
    }
    
    if status != "Success":
        if status == "DataShortage":
             logging.warning(f"⚠️ {symbol} [{timeframe}] クリーンアップ後のデータが短すぎます ({len(ohlcv)}行)。DataShortageとして処理します。")
             
        return {"symbol": symbol, "side": status, "client": client_used, "timeframe": timeframe, "tech_data": tech_data_defaults, "score": 0.5, "price": 0.0, "entry": 0.0, "tp1": 0.0, "sl": 0.0, "rr_ratio": 0.0, "entry_type": "N/A"}


    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = pd.to_numeric(df[col], errors='coerce').astype('float64')
    df.dropna(subset=['high', 'low', 'close', 'volume'], inplace=True)
    
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
    df.set_index('timestamp', inplace=True)
    
    price = df['close'].iloc[-1] if not df.empty and not pd.isna(df['close'].iloc[-1]) else 0.0
    atr_val = price * 0.005 if price > 0 else 0.005 

    final_side = "Neutral"
    base_score = BASE_SCORE 
    macd_valid = False
    current_long_term_penalty_applied = False
    
    MACD_HIST_COL = 'MACD_Hist'     
    PPO_HIST_COL = 'PPOh_12_26_9'   
    STOCHRSI_K = 'STOCHRSIk_14_14_3_3'
    
    try:
        # 2. テクニカル指標の計算
        df['rsi'] = ta.rsi(df['close'], length=14)
        df.ta.macd(append=True) 
        df['adx'] = ta.adx(df['high'], df['low'], df['close'], length=14)['ADX_14']
        df.ta.bbands(close='close', length=20, append=True) 
        df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=14)
        df['cci'] = ta.cci(df['high'], df['low'], df['close'], length=20)
        df.ta.vwap(append=True) 
        df.ta.ppo(append=True) 
        df.ta.donchian(length=20, append=True) 
        df.ta.stochrsi(append=True)
        # Ichimoku Kinko Hyo
        df.ta.ichimoku(append=True) 
        # True Strength Index (TSI)
        df.ta.tsi(append=True) 
        
        pivots = calculate_fib_pivot(df)
        
        required_cols = ['rsi', MACD_HIST_COL, 'adx', 'atr', 'cci', 'VWAP', PPO_HIST_COL, 'BBM_20_2.0', 'TSIs_13_24_6', 'ITS_9', 'IKS_26', 'ISA_26', 'ISB_52'] 
        
        df.dropna(subset=[col for col in required_cols if col in df.columns], inplace=True)
        
        if df.empty or len(df) < 2: 
            return {"symbol": symbol, "side": "DataShortage", "client": client_used, "timeframe": timeframe, "tech_data": tech_data_defaults, "score": 0.5, "price": price, "entry": 0.0, "tp1": 0.0, "sl": 0.0, "rr_ratio": 0.0, "entry_type": "N/A"}

        # 3. 動的シグナル判断ロジック (スコアリング)
        
        rsi_val = df['rsi'].iloc[-1]
        macd_hist_val = df[MACD_HIST_COL].iloc[-1] 
        macd_hist_val_prev = df[MACD_HIST_COL].iloc[-2] 
        adx_val = df['adx'].iloc[-1]
        atr_val = df['atr'].iloc[-1]
        vwap_val = df['VWAP'].iloc[-1] 
        ppo_hist_val = df[PPO_HIST_COL].iloc[-1] 
        bb_width_pct_val = (df['BBU_20_2.0'].iloc[-1] - df['BBL_20_2.0'].iloc[-1]) / df['close'].iloc[-1] * 100 if 'BBU_20_2.0' in df.columns else 0.0
        tsi_val = df['TSIs_13_24_6'].iloc[-1] if 'TSIs_13_24_6' in df.columns else 0.0
        
        # Ichimoku
        ichimoku_cols = ['ITS_9', 'IKS_26', 'ISA_26', 'ISB_52']
        if all(col in df.columns for col in ichimoku_cols):
             ts = df['ITS_9'].iloc[-1]
             ks = df['IKS_26'].iloc[-1]
             span_a = df['ISA_26'].iloc[-1]
             span_b = df['ISB_52'].iloc[-1]
             kumo_high = max(span_a, span_b)
             kumo_low = min(span_a, span_b)
        else:
            ts, ks, kumo_high, kumo_low = price, price, price, price
            
        current_volume = df['volume'].iloc[-1]
        average_volume = df['volume'].iloc[-31:-1].mean() if len(df) >= 31 else df['volume'].mean()
        volume_ratio = current_volume / average_volume if average_volume > 0 else 0.0

        long_score = BASE_SCORE 
        short_score = BASE_SCORE 
        
        dc_cols_present = 'DCL_20' in df.columns and 'DCU_20' in df.columns
        dc_low_val = price 
        dc_high_val = price
        if dc_cols_present:
            dc_low_val = df['DCL_20'].iloc[-1]     
            dc_high_val = df['DCU_20'].iloc[-1]
        
        is_breaking_high = False
        is_breaking_low = False
        if dc_cols_present: 
            is_breaking_high = price > dc_high_val and df['close'].iloc[-2] <= dc_high_val
            is_breaking_low = price < dc_low_val and df['close'].iloc[-2] >= dc_low_val
        
        # A-H スコアリングロジック
        if macd_hist_val > 0 and macd_hist_val > macd_hist_val_prev: long_score += 0.15 
        elif macd_hist_val < 0 and macd_hist_val < macd_hist_val_prev: short_score += 0.15 

        if rsi_val < RSI_OVERSOLD: long_score += 0.08
        elif rsi_val > RSI_OVERBOUGHT: short_score += 0.08
            
        if rsi_val > RSI_MOMENTUM_HIGH and df['rsi'].iloc[-2] <= RSI_MOMENTUM_HIGH: long_score += 0.12 
        elif rsi_val < RSI_MOMENTUM_LOW and df['rsi'].iloc[-2] >= RSI_MOMENTUM_LOW: short_score += 0.12 

        if adx_val > ADX_TREND_THRESHOLD:
            if long_score > short_score: long_score += 0.10
            elif short_score > long_score: short_score += 0.10
        
        vwap_consistent = False
        if price > vwap_val:
            long_score += 0.04
            vwap_consistent = True
        elif price < vwap_val:
            short_score += 0.04
            vwap_consistent = True
        
        ppo_abs_mean = df[PPO_HIST_COL].abs().mean()
        if ppo_hist_val > 0 and abs(ppo_hist_val) > ppo_abs_mean: long_score += 0.04 
        elif ppo_hist_val < 0 and abs(ppo_hist_val) > ppo_abs_mean: short_score += 0.04

        if is_breaking_high: long_score += 0.15 
        elif is_breaking_low: short_score += 0.15
        
        if macd_hist_val > 0 and ppo_hist_val > 0 and rsi_val > 50: long_score += 0.05
        elif macd_hist_val < 0 and ppo_hist_val < 0 and rsi_val < 50: short_score += 0.05
        
        # 最終スコア決定 (中間)
        if long_score > short_score:
            side = "ロング"
            base_score = long_score
        elif short_score > long_score:
            side = "ショート"
            base_score = short_score
        else:
            side = "Neutral"
            base_score = BASE_SCORE
        
        score = min(1.0, base_score) 

        # I. 資金調達率 (Funding Rate) バイアスフィルター
        funding_rate_bonus = 0.0
        if timeframe == '1h': 
            if side == "ロング":
                if funding_rate_val > FUNDING_RATE_THRESHOLD: funding_rate_bonus = -FUNDING_RATE_BONUS_PENALTY
                elif funding_rate_val < -FUNDING_RATE_THRESHOLD: funding_rate_bonus = FUNDING_RATE_BONUS_PENALTY
            elif side == "ショート":
                if funding_rate_val < -FUNDING_RATE_THRESHOLD: funding_rate_bonus = -FUNDING_RATE_BONUS_PENALTY
                elif funding_rate_val > FUNDING_RATE_THRESHOLD: funding_rate_bonus = FUNDING_RATE_BONUS_PENALTY
        score = max(BASE_SCORE, min(1.0, score + funding_rate_bonus))

        # J. BTCドミナンスバイアスフィルター (Altcoinのみ)
        dominance_bonus = 0.0
        dominance_trend = macro_context.get('dominance_trend', 'Neutral')
        
        if symbol != "BTC-USDT" and dominance_trend != "Neutral":
            if dominance_trend == "Increasing": 
                if side == "ロング": dominance_bonus = DOMINANCE_BIAS_PENALTY
                elif side == "ショート": dominance_bonus = -DOMINANCE_BIAS_PENALTY
            elif dominance_trend == "Decreasing": 
                if side == "ロング": dominance_bonus = -DOMINANCE_BIAS_PENALTY
                elif side == "ショート": dominance_bonus = DOMINANCE_BIAS_PENALTY
            score = max(BASE_SCORE, min(1.0, score + dominance_bonus))

        # K. 市場センチメント (FGI Proxy) の適用
        sentiment_bonus = macro_context.get('sentiment_fgi_proxy', 0.0)
        if side == "ロング" and sentiment_bonus > 0: score = min(1.0, score + sentiment_bonus)
        elif side == "ショート" and sentiment_bonus < 0: score = min(1.0, score + abs(sentiment_bonus))
        
        # L. Structural/Pivot Analysis
        structural_pivot_bonus, structural_sl_pivot, structural_tp_pivot = analyze_structural_proximity(price, pivots, side)
        score = min(1.0, score + structural_pivot_bonus)
        
        # M. 出来高/流動性確証
        volume_confirmation_bonus = 0.0
        if volume_ratio >= VOLUME_CONFIRMATION_MULTIPLIER: 
            if dc_cols_present and (is_breaking_high or is_breaking_low): volume_confirmation_bonus += 0.06
            if abs(macd_hist_val) > df[MACD_HIST_COL].abs().mean(): volume_confirmation_bonus += 0.06
            score = min(1.0, score + volume_confirmation_bonus)

        # N. Volatility Adaptive Filter (VAF)
        vaf_penalty_value = 0.0
        if timeframe == '15m' and bb_width_pct_val < 1.0: 
             vaf_penalty_value = 0.10 
             score = max(BASE_SCORE, score - vaf_penalty_value)
        elif timeframe == '1h' and bb_width_pct_val < 1.5: 
             vaf_penalty_value = 0.05
             score = max(BASE_SCORE, score - vaf_penalty_value)
        
        # O. MTF Trend Convergence Bonus (TSC)
        mtf_convergence_bonus = 0.0
        if mtf_convergence_applied:
            mtf_convergence_bonus = MTF_CONVERGENCE_BONUS
            score = min(1.0, score + mtf_convergence_bonus)
            
        # P. Ichimoku Kinko Hyo
        ichimoku_conf = "Neutral"
        ichimoku_bonus = 0.0
        if 'ITS_9' in df.columns:
            if side == "ロング":
                if price > kumo_high: 
                    ichimoku_bonus += 0.05
                if ts > ks and df['ITS_9'].iloc[-2] <= df['IKS_26'].iloc[-2]: 
                    ichimoku_bonus += 0.05
            elif side == "ショート":
                if price < kumo_low: 
                    ichimoku_bonus += 0.05
                if ts < ks and df['ITS_9'].iloc[-2] >= df['IKS_26'].iloc[-2]: 
                    ichimoku_bonus += 0.05
            
            if ichimoku_bonus > 0:
                score = min(1.0, score + ichimoku_bonus)
                ichimoku_conf = side
            
        # Q. True Strength Index (TSI)
        tsi_conf = "Neutral"
        tsi_bonus = 0.0
        if 'TSIs_13_24_6' in df.columns:
            if side == "ロング" and tsi_val > 0 and tsi_val < TSI_OVERBOUGHT:
                tsi_bonus = TSI_BONUS
            elif side == "ショート" and tsi_val < 0 and tsi_val > TSI_OVERSOLD:
                tsi_bonus = TSI_BONUS
                
            if tsi_bonus > 0:
                score = min(1.0, score + tsi_bonus)
                tsi_conf = side
        
        # 4. 4hトレンドフィルターの適用
        penalty_value_lt = 0.0
        if timeframe in ['15m', '1h']:
            if (side == "ロング" and long_term_trend == "Short") or \
               (side == "ショート" and long_term_trend == "Long"):
                score = max(BASE_SCORE, score - LONG_TERM_REVERSAL_PENALTY) 
                current_long_term_penalty_applied = True
                penalty_value_lt = LONG_TERM_REVERSAL_PENALTY
        
        # 5. MACDクロス確認と減点
        macd_valid = True
        penalty_value_macd = 0.0
        if timeframe in ['15m', '1h']:
             is_macd_reversing = (macd_hist_val > 0 and macd_hist_val < macd_hist_val_prev) or \
                                 (macd_hist_val < 0 and macd_hist_val > macd_hist_val_prev)
             
             if is_macd_reversing and score >= SIGNAL_THRESHOLD:
                 score = max(BASE_SCORE, score - MACD_CROSS_PENALTY)
                 macd_valid = False
                 penalty_value_macd = MACD_CROSS_PENALTY
             
        
        # 6. TP/SLとRRRの決定 (v20.0.0 - 適応型TP)
        
        rr_base = TIMEframe_RRR_MAP.get(timeframe, 5.0) 
        
        is_high_conviction = score >= 0.80
        is_strong_trend = adx_val >= 35
        use_market_entry = is_high_conviction and is_strong_trend and timeframe != '15m' 
        entry_type = "Market" if use_market_entry else "Limit"
        
        bb_mid = df['BBM_20_2.0'].iloc[-1] if 'BBM_20_2.0' in df.columns else price
        dc_mid = (df['DCU_20'].iloc[-1] + df['DCL_20'].iloc[-1]) / 2 if dc_cols_present else price
        
        entry = price 
        tp1 = 0
        sl = 0
        
        sl_dist_atr = atr_val * ATR_TRAIL_MULTIPLIER 
        structural_sl_used = False

        pullback_amount = atr_val * ATR_ENTRY_PULLBACK_MULTIPLIER
        
        if side == "ロング":
            if entry_type == "Market": 
                entry = price
            else: 
                entry_base = min(bb_mid, dc_mid) 
                entry = min(entry_base - pullback_amount, price)
            
            atr_sl = entry - sl_dist_atr
            
            # 構造的SL (S1) を使用し、ATRバッファを適用
            if structural_sl_pivot > 0 and structural_sl_pivot > atr_sl and structural_sl_pivot < entry:
                 sl = structural_sl_pivot - atr_val * 0.5 
                 structural_sl_used = True
            else:
                 sl = atr_sl
            
            if sl <= 0: sl = entry * 0.99 
            
            tp_dist = abs(entry - sl) * rr_base 
            tp1 = entry + tp_dist
            
        elif side == "ショート":
            if entry_type == "Market": 
                entry = price
            else: 
                entry_base = max(bb_mid, dc_mid) 
                entry = max(entry_base + pullback_amount, price)
            
            atr_sl = entry + sl_dist_atr
            
            # 構造的SL (R1) を使用し、ATRバッファを適用
            if structural_sl_pivot > 0 and structural_sl_pivot < atr_sl and structural_sl_pivot > entry:
                 sl = structural_sl_pivot + atr_val * 0.5 
                 structural_sl_used = True
            else:
                 sl = atr_sl
                 
            tp_dist = abs(entry - sl) * rr_base 
            tp1 = entry - tp_dist
            
        else:
            entry_type = "N/A"
            entry, tp1, sl, rr_base = price, 0, 0, 0
        
        # 7. 最終的なサイドの決定
        final_side = side
        if score < SIGNAL_THRESHOLD: 
             final_side = "Neutral"

        # 8. tech_dataの構築
        tech_data = {
            "rsi": rsi_val, "macd_hist": macd_hist_val, "adx": adx_val,
            "bb_width_pct": bb_width_pct_val, "atr_value": atr_val,
            "long_term_trend": long_term_trend,
            "long_term_reversal_penalty": current_long_term_penalty_applied,
            "macd_cross_valid": macd_valid, "cci": df['cci'].iloc[-1], 
            "vwap_consistent": vwap_consistent, "ppo_hist": ppo_hist_val, 
            "dc_high": dc_high_val, "dc_low": dc_low_val,
            "stoch_k": df[STOCHRSI_K].iloc[-1] if STOCHRSI_K in df.columns else 50.0, 
            "stoch_d": df['STOCHRSId_14_14_3_3'].iloc[-1] if 'STOCHRSId_14_14_3_3' in df.columns else 50.0,
            "stoch_filter_penalty": tech_data_defaults["stoch_filter_penalty"], 
            "volume_confirmation_bonus": volume_confirmation_bonus,
            "current_volume": current_volume, "average_volume": average_volume,
            "sentiment_fgi_proxy_bonus": sentiment_bonus,
            "structural_pivot_bonus": structural_pivot_bonus,
            "volume_ratio": volume_ratio,
            "structural_sl_used": structural_sl_used, 
            "long_term_reversal_penalty_value": penalty_value_lt,
            "macd_cross_penalty_value": penalty_value_macd,
            "funding_rate_value": funding_rate_val,
            "funding_rate_bonus_value": funding_rate_bonus,
            "dominance_trend": dominance_trend,
            "dominance_bias_bonus_value": dominance_bonus,
            "dynamic_exit_strategy": "DTS",
            "vaf_penalty_value": vaf_penalty_value, 
            "mtf_convergence_bonus": mtf_convergence_bonus,
            "ichimoku_confirmation": ichimoku_conf,
            "ichimoku_bonus": ichimoku_bonus,
            "tsi": tsi_val,
            "tsi_confirmation": tsi_conf,
            "tsi_bonus": tsi_bonus,
        }
        
    except Exception as e:
        logging.warning(f"⚠️ {symbol} ({timeframe}) のテクニカル分析中に予期せぬエラーが発生しました: {e} ({type(e).__name__}). Neutralとして処理を継続します。")
        final_side = "Neutral"
        score = 0.5
        entry, tp1, sl, rr_base = price, 0, 0, 0 
        tech_data = tech_data_defaults 
        entry_type = "N/A"
        
    # 9. シグナル辞書を構築
    signal_candidate = {
        "symbol": symbol, "side": final_side, "score": score, 
        "confidence": score, "price": price, "entry": entry,
        "tp1": tp1, "sl": sl,   
        "rr_ratio": rr_base if final_side != "Neutral" else 0.0,
        "regime": "トレンド" if tech_data['adx'] >= ADX_TREND_THRESHOLD else "レンジ",
        "macro_context": macro_context,
        "client": client_used, "timeframe": timeframe,
        "tech_data": tech_data,
        "volatility_penalty_applied": tech_data['bb_width_pct'] > VOLATILITY_BB_PENALTY_THRESHOLD,
        "entry_type": entry_type
    }
    
    return signal_candidate

# ====================================================================================
# MACRO CONTEXT & SIGNAL INTEGRATION
# ====================================================================================

async def get_crypto_macro_context() -> Dict:
    """
    BTCドミナンスのトレンドと市場センチメントの代理値を計算する。
    """
    
    # 1. BTC Dominance Trend Analysis (代理としてBTC.Dの過去24時間変動を使用)
    dominance_trend = "Neutral"
    
    try:
        btc_d_data = yf.download('BTC-USD', period='5d', interval='1d', prepost=False, progress=False)
        btc_d_data = btc_d_data.dropna(subset=['Close'])
        
        if len(btc_d_data) >= 2:
            current_close = btc_d_data['Close'].iloc[-1]
            prev_close = btc_d_data['Close'].iloc[-2]
            
            # ドミナンス増加トレンド（昨日の終値より今日の終値が高い）
            if current_close > prev_close:
                dominance_trend = "Increasing"
            # ドミナンス減少トレンド
            elif current_close < prev_close:
                dominance_trend = "Decreasing"

    except Exception as e:
        logging.warning(f"BTCドミナンスデータの取得エラー: {e}")

    # 2. Fear & Greed Index (FGI) Proxy (ここではランダム値を使用 - 外部APIに依存しないため)
    # 実装では外部FGI APIを使用することを推奨
    fgi_proxy = random.uniform(-0.07, 0.07) # -0.07から+0.07の範囲でランダムなボーナス/ペナルティ
    
    return {
        "dominance_trend": dominance_trend,
        "sentiment_fgi_proxy": fgi_proxy 
    }

async def get_top_30_symbols(client_name: str) -> List[str]:
    """取引所から出来高トップ30のシンボルリストを取得する (プレースホルダー)"""
    # リアルタイムの出来高ランキング取得は取引所APIに依存するため、ここではデフォルトリストを返す
    # 実際の運用ではCCXTのfetch_tickersなどを使用して出来高でソートする
    
    # OKXなどのCCXTクライアントでUSDTペアを取得するロジックを想定
    try:
        if not EXCHANGE_CLIENT:
            await initialize_client(client_name)
        
        markets = await EXCHANGE_CLIENT.load_markets()
        
        usdt_markets = [
            symbol 
            for symbol, market in markets.items() 
            if market['active'] and 'USDT' in symbol and (market['type'] == 'future' or market['type'] == 'swap')
        ]
        
        # 出来高でのソートは複雑なため、ここではデフォルトのリストを使用し、CCXT形式に変換
        top_symbols = [s.replace('/', '-') for s in DEFAULT_SYMBOLS if s.replace('/', '-') in [m.replace('/', '-') for m in usdt_markets]]
        
        return top_symbols[:TOP_SYMBOL_LIMIT]
        
    except Exception as e:
        logging.warning(f"シンボルリストの取得エラー、デフォルトリストを使用します: {e}")
        return [s.replace('/', '-') for s in DEFAULT_SYMBOLS[:TOP_SYMBOL_LIMIT]]


async def generate_integrated_signal(symbol: str, macro_context: Dict, client_name: str) -> List[Dict]:
    """
    複数の時間軸で分析を行い、統合されたシグナルを生成する。
    """
    
    timeframes = ['4h', '1h', '15m']
    all_signals: List[Dict] = []
    
    # 4h足のトレンドを最初に取得
    ohlcv_4h, status_4h, client_4h = await fetch_ohlcv_with_fallback(client_name, symbol, '4h')
    
    long_term_trend = 'Neutral'
    if status_4h == "Success":
        df_4h = pd.DataFrame(ohlcv_4h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df_4h['close'] = pd.to_numeric(df_4h['close'], errors='coerce').astype('float64')
        df_4h['sma_50'] = df_4h['close'].rolling(window=50).mean()
        
        last_price = df_4h['close'].iloc[-1]
        last_sma = df_4h['sma_50'].iloc[-1]
        
        if last_price > last_sma:
            long_term_trend = 'Long'
        elif last_price < last_sma:
            long_term_trend = 'Short'
            
    
    # 15m, 1h, 4hの分析を並列実行
    tasks = []
    for tf in timeframes:
        # MTF Convergence Check
        mtf_convergence_applied = False
        if tf != '4h':
             if (tf == '1h' and long_term_trend != 'Neutral') or \
                (tf == '15m' and long_term_trend != 'Neutral'):
                 mtf_convergence_applied = True 
                 
        tasks.append(
            analyze_single_timeframe(
                symbol, tf, macro_context, client_name, long_term_trend, long_term_trend, mtf_convergence_applied
            )
        )

    results = await asyncio.gather(*tasks)
    all_signals = [r for r in results if r is not None]
    
    return all_signals

# ====================================================================================
# MAIN LOOP & EXECUTION
# ====================================================================================

async def main_loop():
    """BOTのメイン実行ループ"""
    global LAST_UPDATE_TIME, CURRENT_MONITOR_SYMBOLS, TRADE_NOTIFIED_SYMBOLS, LAST_ANALYSIS_SIGNALS, LAST_SUCCESS_TIME, GLOBAL_MACRO_CONTEXT
    
    await initialize_client(CCXT_CLIENT_NAME)

    while True:
        start_time = time.time()
        
        try:
            # 1. BTCドミナンスなど、マクロコンテキストの取得
            GLOBAL_MACRO_CONTEXT = await get_crypto_macro_context()
            logging.info(f"マクロコンテキスト更新。Dominance Trend: {GLOBAL_MACRO_CONTEXT.get('dominance_trend')}")
            
            # 2. モニタリングシンボルの更新 (30分に一度)
            if time.time() - LAST_UPDATE_TIME > 60 * 30: 
                new_symbols = await get_top_30_symbols(CCXT_CLIENT_NAME)
                if new_symbols:
                    CURRENT_MONITOR_SYMBOLS = new_symbols
                LAST_UPDATE_TIME = time.time()
                logging.info(f"🔄 モニタリングシンボルを更新しました ({len(CURRENT_MONITOR_SYMBOLS)} 銘柄)")
                
            # 3. 全シンボルに対するシグナル分析
            tasks = []
            for symbol in CURRENT_MONITOR_SYMBOLS:
                tasks.append(generate_integrated_signal(symbol, GLOBAL_MACRO_CONTEXT, CCXT_CLIENT_NAME))
            
            all_results = await asyncio.gather(*tasks)
            
            # 4. 高確度シグナルのフィルタリングとランキング
            high_conviction_signals: List[Dict] = []
            
            for symbol_signals in all_results:
                for sig in symbol_signals:
                    if sig.get('side') not in ["DataShortage", "ExchangeError", "Neutral"] and sig.get('score', 0.0) >= SIGNAL_THRESHOLD:
                         high_conviction_signals.append(sig)

            # スコア、RRR、ADXの順でランキング
            ranked_signals = sorted(
                high_conviction_signals, 
                key=lambda s: (
                    s.get('score', 0.0), 
                    s.get('rr_ratio', 0.0), 
                    s.get('tech_data', {}).get('adx', 0.0)
                ), 
                reverse=True
            )
            
            LAST_ANALYSIS_SIGNALS = ranked_signals
            
            # 5. 通知
            notification_count = 0
            
            unique_notified_symbols = set()
            
            for rank, signal in enumerate(ranked_signals[:TOP_SIGNAL_COUNT]):
                symbol = signal['symbol']
                
                # クールダウンチェック
                if symbol not in TRADE_NOTIFIED_SYMBOLS or time.time() - TRADE_NOTIFIED_SYMBOLS[symbol] >= TRADE_SIGNAL_COOLDOWN:
                    
                    # 既に同じシンボルのシグナルが通知対象になっていればスキップ (ランキング最上位のみ採用)
                    if symbol in unique_notified_symbols:
                        continue
                        
                    # 当該シンボルの全時間軸の結果を取得
                    target_signals = [s for s_list in all_results for s in s_list if s.get('symbol') == symbol]
                    
                    # メッセージの整形と送信 (v21.0.0でハイパー・ビジュアル化されたもの)
                    message = format_integrated_analysis_message(symbol, target_signals, rank + 1)
                    
                    if message and send_telegram_html(message):
                        TRADE_NOTIFIED_SYMBOLS[symbol] = time.time()
                        unique_notified_symbols.add(symbol)
                        notification_count += 1
                        
            
            logging.info(f"✅ 総合分析完了。高確度シグナル {len(ranked_signals)} 件、新規通知 {notification_count} 件。")
            
            LAST_SUCCESS_TIME = time.time()
            
        except Exception as e:
            error_name = type(e).__name__
            logging.error(f"メインループで致命的なエラー: {error_name}: {e}")
            
        
        # 実行時間の計算とインターバル調整
        elapsed_time = time.time() - start_time
        sleep_duration = max(0, LOOP_INTERVAL - elapsed_time)
        
        if sleep_duration > 0:
            logging.info(f"😴 次の実行まで {sleep_duration:.1f} 秒待機します...")
            await asyncio.sleep(sleep_duration)
        else:
            logging.warning("⚠️ 処理時間がループ間隔を超過しました。即時再実行します。")


# ====================================================================================
# FASTAPI SETUP
# ====================================================================================

app = FastAPI(title="Apex BOT API", version="v21.0.0") 

@app.on_event("startup")
async def startup_event():
    logging.info("🚀 Apex BOT v21.0.0 Startup initializing...") 
    # メインループをバックグラウンドで実行
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
        "bot_version": app.version, 
        "last_success_time_utc": datetime.fromtimestamp(LAST_SUCCESS_TIME, tz=timezone.utc).isoformat() if LAST_SUCCESS_TIME else "N/A",
        "current_client": CCXT_CLIENT_NAME,
        "monitoring_symbols": len(CURRENT_MONITOR_SYMBOLS),
        "last_signals_count": len(LAST_ANALYSIS_SIGNALS)
    }
    return JSONResponse(content=status_msg)

@app.head("/")
@app.get("/")
def home_view():
    return JSONResponse(content={"message": "Apex BOT is running (v21.0.0, PnL Projection Edition)."}, status_code=200)

if __name__ == '__main__':
    # この部分を削除して、Uvicornで実行する必要があります。
    # 例: uvicorn main:app --host 0.0.0.0 --port 8000
    pass
