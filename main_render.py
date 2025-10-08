# ====================================================================================
# Apex BOT v19.0.4 - DTS & Dominance Bias Filter (Cooldown Removed)
# - NEW: TRADE_SIGNAL_COOLDOWN を完全に撤廃し、分析サイクルごとにスコアが閾値を超えたシグナルを即時通知するよう変更。
# - FIX: BadSymbolエラーで除外されたシンボルを代替候補で自動的に埋め、常に30銘柄を監視する機能を維持。
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

# 出来高TOP30に加えて、主要な基軸通貨をDefaultに含めておく (60銘柄に拡張)
DEFAULT_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "ADA/USDT", "XRP/USDT", "DOT/USDT", 
    "DOGE/USDT", "AVAX/USDT", "LINK/USDT", "LTC/USDT", "MATIC/USDT", "TRX/USDT", 
    "ATOM/USDT", "NEAR/USDT", "ALGO/USDT", "XLM/USDT", "BCH/USDT", "ETC/USDT", 
    "UNI/USDT", "ICP/USDT", "FIL/USDT", "AAVE/USDT", "AXS/USDT", "SAND/USDT",
    "GALA/USDT", "FTM/USDT", "HBAR/USDT", "VET/USDT", "GRT/USDT", "SHIB/USDT",
    # 31-60. 候補を拡張 (BadSymbol分を埋めるバッファ)
    "MKR/USDT", "RUNE/USDT", "WLD/USDT", "PEPE/USDT", "ARB/USDT", "OP/USDT",
    "INJ/USDT", "TIA/USDT", "SUI/USDT", "SEI/USDT", "KAS/USDT", "MINA/USDT",
    "APT/USDT", "RDNT/USDT", "DYDX/USDT", "EOS/USDT", "ZEC/USDT", "KNC/USDT",
    "GMX/USDT", "SNX/USDT", "CRV/USDT", "BAL/USDT", "COMP/USDT", "FET/USDT",
    "AGIX/USDT", "OCEAN/USDT", "IMX/USDT", "MASK/USDT", "GTC/USDT", "ZIL/USDT"
] 
FINAL_MONITORING_LIMIT = 30  # 最終的に監視するシンボル数 (常に30を維持)
INITIAL_CANDIDATE_LIMIT = 60 # 初期候補リストのサイズ (BadSymbol分を埋めるバッファ)

LOOP_INTERVAL = 180        
REQUEST_DELAY_PER_SYMBOL = 0.5 

# 環境変数から取得。未設定の場合はダミー値。
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', 'YOUR_TELEGRAM_CHAT_ID')

# TRADE_SIGNAL_COOLDOWN は撤廃されました (v19.0.4)
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
STOCH_RSI_OVERSOLD = 10 
STOCH_RSI_OVERBOUGHT = 90
STOCH_FILTER_PENALTY = 0.10


# グローバル状態変数
CCXT_CLIENT_NAME: str = 'OKX' 
EXCHANGE_CLIENT: Optional[ccxt_async.Exchange] = None
LAST_UPDATE_TIME: float = 0.0
CURRENT_MONITOR_SYMBOLS: List[str] = [s.replace('/', '-') for s in DEFAULT_SYMBOLS[:FINAL_MONITORING_LIMIT]] 
# TRADE_NOTIFIED_SYMBOLS は、クールダウン撤廃に伴い、通知チェックには使用されなくなりました。
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
    3つの時間軸の分析結果を統合し、ログメッセージの形式に整形する (v19.0.4対応)
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
    # 1. ヘッダーとエントリー情報の可視化
    # ----------------------------------------------------
    direction_emoji = "🚀 **ロング (LONG)**" if side == "LONG" else "💥 **ショート (SHORT)**"
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
        sl_source_str = "構造的 (Pivot) + **0.5 ATR バッファ**" 
        
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
        f"| ⚙️ **BOT Ver** | **v19.0.4** - Cooldown Removed |\n" # バージョン更新
        f"==================================\n"
        f"\n<pre>※ Limit注文は、価格が指定水準に到達した際のみ約定します。DTS戦略では、価格が有利な方向に動いた場合、SLが自動的に追跡され利益を最大化します。</pre>"
    )

    return header + trade_plan + analysis_detail + footer


# ====================================================================================
# CCXT & DATA ACQUISITION
# ====================================================================================

# ------------------------------------------------------------------------------------
# NEW/FIX: シンボルフィルタリング機能 (v19.0.3 - BadSymbol バッファリング)
# ------------------------------------------------------------------------------------
async def filter_monitoring_symbols(exchange: ccxt_async.Exchange):
    """
    ロードされた市場情報に基づき、DEFAULT_SYMBOLSから実際に存在する先物/スワップシンボルに絞り込む。
    INITIAL_CANDIDATE_LIMITから試行し、有効なシンボル数がFINAL_MONITORING_LIMITになるように埋める。
    """
    global CURRENT_MONITOR_SYMBOLS, LAST_SUCCESSFUL_MONITOR_SYMBOLS, INITIAL_CANDIDATE_LIMIT, FINAL_MONITORING_LIMIT
    
    if not exchange.markets:
        logging.warning("市場情報がロードされていません。シンボルフィルタリングをスキップします。")
        return

    # フィルタリングする初期候補リスト (FINAL_MONITORING_LIMITよりも多く試行)
    initial_symbols = [s.replace('/', '-') for s in DEFAULT_SYMBOLS[:INITIAL_CANDIDATE_LIMIT]]
    
    # 実際に存在する先物/スワップシンボルのベース通貨 (例: 'MATIC') を収集
    available_base_pairs = set()
    for symbol, market in exchange.markets.items():
        # 'future'または'swap'であること、かつUSDT建てであることを確認
        is_contract = market.get('contract', False)
        is_swap = market.get('swap', False)
        
        # USDT建てであることと、contract/swapのフラグを確認
        if (is_contract or is_swap) and market.get('quote') == 'USDT':
            base_symbol = market.get('base') 
            # OKXでは通常、パーペチュアルスワップは 'Base-Quote-SWAP' の形式だが、
            # CCXTのfetch_ohlcvは 'Base-Quote' (例: BTC-USDT) をデフォルトでスワップとして扱うため、ベースシンボルのみをチェックする
            if base_symbol and 'USDT' not in base_symbol: # USDT自体をベース通貨とする変なペアを除く
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

    # 最終的な監視リストをFINAL_MONITORING_LIMITに制限
    final_list = new_monitoring_symbols[:FINAL_MONITORING_LIMIT]
    
    if len(final_list) < FINAL_MONITORING_LIMIT:
        logging.warning(f"有効なシンボルが {FINAL_MONITORING_LIMIT} 件に満たしませんでした。現在の有効シンボル数: {len(final_list)}件")

    if removed_symbols:
        # 実際に存在しなかったシンボルを報告 (初回起動時のBadSymbol対策)
        logging.warning(f"以下のシンボルはOKXの先物/スワップ市場で見つかりませんでした (BadSymbolエラー対策): {', '.join([s for s in removed_symbols if s not in final_list])}")
    
    if not final_list:
         logging.error("有効な先物/スワップシンボルが見つかりませんでした。デフォルトのシンボルリストを確認してください。")
         # エラー時に直前の成功リストにフォールバック、それもなければ初期候補の先頭30に戻す
         final_list = LAST_SUCCESSFUL_MONITOR_SYMBOLS or [s.replace('/', '-') for s in DEFAULT_SYMBOLS[:FINAL_MONITORING_LIMIT]] 
         
    CURRENT_MONITOR_SYMBOLS = final_list
    LAST_SUCCESSFUL_MONITOR_SYMBOLS = final_list.copy() # 成功リストを更新
    logging.info(f"監視対象のシンボルリストを更新しました。有効なシンボル数: {len(CURRENT_MONITOR_SYMBOLS)}/{FINAL_MONITORING_LIMIT}")


async def initialize_ccxt_client():
    """CCXTクライアントを初期化 (OKX)"""
    global EXCHANGE_CLIENT
    
    EXCHANGE_CLIENT = ccxt_async.okx({
        'timeout': 30000, 
        'enableRateLimit': True,
        'options': {'defaultType': 'swap'} 
    })
    
    if EXCHANGE_CLIENT:
        try:
            # OKXの全市場情報をロード
            await EXCHANGE_CLIENT.load_markets()
            logging.info(f"CCXTクライアント ({CCXT_CLIENT_NAME}) を初期化しました。市場情報 ({len(EXCHANGE_CLIENT.markets)}件) をロードしました。")
            
            # v19.0.3: 市場情報に基づいて監視対象シンボルをフィルタリング
            await filter_monitoring_symbols(EXCHANGE_CLIENT)

        except Exception as e:
            logging.error(f"CCXTクライアントの初期化または市場情報のロードに失敗しました: {e}")
            EXCHANGE_CLIENT = None
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
        # OKXのフューチャー/スワップは `instType='SWAP'` を指定
        funding_rate = await EXCHANGE_CLIENT.fetch_funding_rate(symbol, params={'instType': 'SWAP'})
        return funding_rate.get('fundingRate', 0.0) if funding_rate else 0.0
    except Exception as e:
        logging.debug(f"資金調達率の取得に失敗: {symbol}, {e}")
        return 0.0

async def update_symbols_by_volume():
    """CCXTを使用してOKXの出来高トップ30のUSDTペア銘柄を動的に取得・更新する"""
    global CURRENT_MONITOR_SYMBOLS, EXCHANGE_CLIENT, LAST_SUCCESSFUL_MONITOR_SYMBOLS
    global FINAL_MONITORING_LIMIT
    
    if not EXCHANGE_CLIENT:
        return

    try:
        # 出来高の基準は、現物市場の出来高TOP30を採用
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
        
        # 出来高TOPの現物シンボルリスト (例: ['BTC/USDT', 'ETH/USDT', ...])
        top_spot_symbols = [symbol for symbol, _ in sorted_tickers]
        
        # 1. 現物シンボルをスワップシンボル形式に変換 (例: BTC-USDT)
        candidate_swap_symbols = [convert_symbol_to_okx_swap(symbol) for symbol in top_spot_symbols]
        
        # 2. 実際に存在するスワップシンボルにフィルタリング (v19.0.3のロジックを流用)
        available_base_pairs = set()
        for symbol, market in EXCHANGE_CLIENT.markets.items():
            is_contract = market.get('contract', False)
            is_swap = market.get('swap', False)
            if (is_contract or is_swap) and market.get('quote') == 'USDT':
                base_symbol = market.get('base')
                if base_symbol and 'USDT' not in base_symbol:
                    available_base_pairs.add(base_symbol)

        new_monitor_symbols = []
        for symbol_hyphen in candidate_swap_symbols:
            base_symbol_check = symbol_hyphen.split('-')[0]
            if base_symbol_check in available_base_pairs:
                new_monitor_symbols.append(symbol_hyphen)
            if len(new_monitor_symbols) >= FINAL_MONITORING_LIMIT:
                break

        # 最終リストをFINAL_MONITORING_LIMITで制限
        final_list = new_monitor_symbols[:FINAL_MONITORING_LIMIT]
        
        if final_list and len(final_list) == FINAL_MONITORING_LIMIT:
            CURRENT_MONITOR_SYMBOLS = final_list
            LAST_SUCCESSFUL_MONITOR_SYMBOLS = final_list.copy()
            logging.info(f"出来高TOP {FINAL_MONITORING_LIMIT} のシンボルに更新しました。")
        else:
            CURRENT_MONITOR_SYMBOLS = LAST_SUCCESSFUL_MONITOR_SYMBOLS
            logging.warning(f"⚠️ 出来高による更新で有効なシンボルを {FINAL_MONITORING_LIMIT} 件取得できませんでした ({len(final_list)}件)。前回成功したリストを使用します。")
            
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
        # OKXスワップのデフォルト設定 ('options': {'defaultType': 'swap'}) を使用
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=limit)
        if not ohlcv or len(ohlcv) < 30:
            return [], "DataShortage", client_name
        return ohlcv, "Success", client_name
    except ccxt.NetworkError as e:
        return [], "ExchangeError", client_name
    except ccxt.ExchangeError as e:
        # BadSymbolエラーが発生した場合、このシンボルはfilter_monitoring_symbolsで除外されているはずだが、念のため。
        return [], "ExchangeError", client_name
    except Exception as e:
        return [], "ExchangeError", client_name

async def get_crypto_macro_context() -> Dict:
    """ 
    マクロ市場コンテキストを取得 (FGI Proxy, BTC/ETH Trend, Dominance Bias) 
    """
    global GLOBAL_MACRO_CONTEXT, EXCHANGE_CLIENT, CCXT_CLIENT_NAME
    
    new_context = {}
    
    # 1. BTC/USDTとETH/USDTの長期トレンドと直近の価格変化率を取得 (4h足)
    btc_ohlcv, status_btc, _ = await fetch_ohlcv_with_fallback(
        CCXT_CLIENT_NAME, 'BTC-USDT', '4h'
    )
    eth_ohlcv, status_eth, _ = await fetch_ohlcv_with_fallback(
        CCXT_CLIENT_NAME, 'ETH-USDT', '4h'
    )
    
    # 2. Fear & Greed IndexのProxy値を計算 (BTCの4h RSIで代用)
    if status_btc == 'Success':
        df_btc = pd.DataFrame(btc_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df_btc['rsi'] = ta.rsi(df_btc['close'], length=14)
        latest_btc_rsi = df_btc['rsi'].iloc[-1]
        # RSI 50を中立として、RSI 30/70を恐怖/強欲の最大値とするproxy
        # 恐怖: RSI 50 -> 30 (0.0 -> -1.0)
        # 強欲: RSI 50 -> 70 (0.0 -> +1.0)
        sentiment_fgi_proxy = min(1.0, max(-1.0, (latest_btc_rsi - 50) / 20))
        
        new_context['sentiment_fgi_proxy'] = sentiment_fgi_proxy
    else:
        new_context['sentiment_fgi_proxy'] = 0.0
        
    # 3. BTC/ETHのトレンド判定 (4h SMA/MACD)
    btc_trend = 'Neutral'
    eth_trend = 'Neutral'
    if status_btc == 'Success':
        df_btc['sma_long'] = ta.sma(df_btc['close'], length=LONG_TERM_SMA_LENGTH)
        df_btc['macd'] = ta.macd(df_btc['close'])['MACDh_12_26_9']
        latest_close = df_btc['close'].iloc[-1]
        latest_sma = df_btc['sma_long'].iloc[-1]
        
        if latest_close > latest_sma and df_btc['macd'].iloc[-1] > 0:
            btc_trend = 'Long'
        elif latest_close < latest_sma and df_btc['macd'].iloc[-1] < 0:
            btc_trend = 'Short'
            
    if status_eth == 'Success':
        df_eth = pd.DataFrame(eth_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df_eth['sma_long'] = ta.sma(df_eth['close'], length=LONG_TERM_SMA_LENGTH)
        df_eth['macd'] = ta.macd(df_eth['close'])['MACDh_12_26_9']
        latest_close = df_eth['close'].iloc[-1]
        latest_sma = df_eth['sma_long'].iloc[-1]

        if latest_close > latest_sma and df_eth['macd'].iloc[-1] > 0:
            eth_trend = 'Long'
        elif latest_close < latest_sma and df_eth['macd'].iloc[-1] < 0:
            eth_trend = 'Short'
            
    new_context['btc_4h_trend'] = btc_trend
    new_context['eth_4h_trend'] = eth_trend
    
    # 4. BTCドミナンスの動向を取得 (yfinance/TradingViewデータで代用)
    try:
        # yfinanceを使ってBTC.D (ドミナンス) のデータを取得 (代替手法)
        dom_df = yf.download("BTC.D", period="5d", interval="1h", progress=False)
        if not dom_df.empty and len(dom_df) >= 50:
            dom_df.columns = [c.lower() for c in dom_df.columns]
            dom_df['sma_long'] = ta.sma(dom_df['close'], length=50)
            
            latest_close = dom_df['close'].iloc[-1]
            latest_sma = dom_df['sma_long'].iloc[-1]
            
            dom_trend = 'Neutral'
            if latest_close > latest_sma:
                dom_trend = 'Up'
            elif latest_close < latest_sma:
                dom_trend = 'Down'
                
            new_context['dominance_trend'] = dom_trend
            new_context['dominance_data_count'] = len(dom_df)
            
        else:
            new_context['dominance_trend'] = 'Neutral'
            new_context['dominance_data_count'] = 0
            
    except Exception as e:
        logging.warning(f"BTCドミナンスデータの取得に失敗しました: {e}")
        new_context['dominance_trend'] = 'Neutral'
        new_context['dominance_data_count'] = 0

    GLOBAL_MACRO_CONTEXT = new_context
    logging.info(f"マクロコンテキストを更新しました。FR/Dominanceデータ: {new_context.get('dominance_data_count', 0)}件")
    return new_context


# ====================================================================================
# TECHNICAL ANALYSIS & SCORING
# ====================================================================================

def get_long_term_trend(df: pd.DataFrame) -> str:
    """長期SMAとMACDヒストグラムに基づきトレンドを判定する"""
    df['sma_long'] = ta.sma(df['close'], length=LONG_TERM_SMA_LENGTH)
    df['macd'] = ta.macd(df['close'])['MACDh_12_26_9']
    
    latest_close = df['close'].iloc[-1]
    latest_sma = df['sma_long'].iloc[-1]
    latest_macd = df['macd'].iloc[-1]
    
    if latest_close > latest_sma and latest_macd > 0:
        return 'Long'
    elif latest_close < latest_sma and latest_macd < 0:
        return 'Short'
    else:
        return 'Neutral'

def calculate_technical_indicators(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """必要なテクニカル指標を計算する"""
    # ATR for stop-loss and RRR
    df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=14)
    # RSI for momentum
    df['rsi'] = ta.rsi(df['close'], length=14)
    # MACD for momentum
    macd_df = ta.macd(df['close'])
    df['macd_hist'] = macd_df['MACDh_12_26_9']
    # ADX for regime filter
    adx_df = ta.adx(df['high'], df['low'], df['close'], length=14)
    df['adx'] = adx_df['ADX_14']
    df['di_plus'] = adx_df['DMP_14']
    df['di_minus'] = adx_df['DMN_14']
    # CCI for mean reversion
    df['cci'] = ta.cci(df['high'], df['low'], df['close'], length=20)
    # VWAP (Volume-Weighted Average Price)
    # VWAPは当日のデータのみに依存するため、ここでは簡易SMAで代用するか、日中取引のVWAP計算ロジックが必要。
    # ここでは、ボリュームと価格の相関を見るために、ボリューム加重SMAを使用
    df['vwap_proxy'] = (df['close'] * df['volume']).rolling(window=20).sum() / df['volume'].rolling(window=20).sum()
    # Stochastic RSI
    stoch_rsi_df = ta.stochrsi(df['close'], length=14, rsi_length=14, k=3, d=3)
    df['stoch_k'] = stoch_rsi_df['STOCHRSIk_14_14_3_3']
    df['stoch_d'] = stoch_rsi_df['STOCHRSId_14_14_3_3']
    
    # Bollinger Bands for volatility filter
    bbands = ta.bbands(df['close'], length=20, std=2)
    df['bb_std'] = bbands['BBL_20_2.0'] - bbands['BBU_20_2.0'] # Upper - Lower 
    
    # Volume confirmation (Volume Ratio)
    df['volume_sma'] = ta.sma(df['volume'], length=20)
    df['volume_ratio'] = df['volume'] / df['volume_sma']
    
    # Pivot Points (Floor Pivots)
    def calculate_pivots(high, low, close):
        P = (high + low + close) / 3
        R1 = 2 * P - low
        S1 = 2 * P - high
        return R1, S1

    df['R1'], df['S1'] = zip(*df.apply(lambda row: calculate_pivots(row['high'], row['low'], row['close']), axis=1))

    return df.dropna().copy()


def calculate_trade_score(
    df: pd.DataFrame, 
    timeframe: str, 
    long_term_trend: str, 
    funding_rate: float, 
    macro_context: Dict
) -> Tuple[str, float, Dict]:
    """
    テクニカル指標に基づき、ロングとショートのシグナルスコアを計算する。
    トレンド、モメンタム、構造、マクロコンテキスト、流動性を加味する。
    """
    if df.empty:
        return "Neutral", BASE_SCORE, {}

    latest = df.iloc[-1]
    
    # ----------------------------------------
    # 1. Regime/ADX Filter & Base Score Initialization
    # ----------------------------------------
    adx = latest['adx']
    rsi = latest['rsi']
    macd_hist = latest['macd_hist']
    close_price = latest['close']
    
    regime = 'Trend' if adx >= ADX_TREND_THRESHOLD else 'Range'
    
    # スコアリングの開始点 (BASE_SCORE = 0.40)
    long_score = BASE_SCORE
    short_score = BASE_SCORE
    tech_data = {'adx': adx, 'regime': regime}
    
    # ----------------------------------------
    # 2. Momentum & Mean Reversion (RSI/MACD/CCI/StochRSI)
    # ----------------------------------------
    long_momentum_bonus = 0.0
    short_momentum_bonus = 0.0

    # RSI (Mean Reversion / Momentum)
    if regime == 'Range':
        if rsi <= RSI_OVERSOLD:
            long_momentum_bonus += 0.10 # 極端な売られすぎ
        elif rsi >= RSI_OVERBOUGHT:
            short_momentum_bonus += 0.10 # 極端な買われすぎ
            
        if rsi < RSI_MOMENTUM_LOW:
            long_momentum_bonus += 0.05
        elif rsi > RSI_MOMENTUM_HIGH:
            short_momentum_bonus += 0.05
            
    elif regime == 'Trend':
        if rsi > RSI_MOMENTUM_HIGH: # 強いトレンド中の過熱
            long_momentum_bonus += 0.08
        elif rsi < RSI_MOMENTUM_LOW:
            short_momentum_bonus += 0.08
            
    # MACD Hist (Cross Confirmation)
    macd_cross_valid = True
    macd_cross_penalty_value = 0.0
    if macd_hist > 0 and latest['macd_hist_1'] < 0 and long_term_trend == 'Short': # 逆張りのモメンタム転換
        macd_cross_valid = False
        long_score -= MACD_CROSS_PENALTY
        macd_cross_penalty_value = MACD_CROSS_PENALTY
    elif macd_hist < 0 and latest['macd_hist_1'] > 0 and long_term_trend == 'Long':
        macd_cross_valid = False
        short_score -= MACD_CROSS_PENALTY
        macd_cross_penalty_value = MACD_CROSS_PENALTY
        
    tech_data['macd_cross_valid'] = macd_cross_valid
    tech_data['macd_cross_penalty_value'] = macd_cross_penalty_value

    # CCI (Overbought/Oversold)
    cci = latest['cci']
    if cci < -100:
        long_momentum_bonus += 0.05
    elif cci > 100:
        short_momentum_bonus += 0.05

    # StochRSI (Filter) - 15m/1hで過熱感をフィルタリング
    stoch_k = latest['stoch_k']
    stoch_d = latest['stoch_d']
    stoch_filter_penalty = 0.0

    if timeframe in ['15m', '1h']:
        if stoch_k > STOCH_RSI_OVERBOUGHT and stoch_d > STOCH_RSI_OVERBOUGHT:
            long_score -= STOCH_FILTER_PENALTY
            stoch_filter_penalty = STOCH_FILTER_PENALTY
        elif stoch_k < STOCH_RSI_OVERSOLD and stoch_d < STOCH_RSI_OVERSOLD:
            short_score -= STOCH_FILTER_PENALTY
            stoch_filter_penalty = STOCH_FILTER_PENALTY
            
    tech_data['stoch_filter_penalty'] = stoch_filter_penalty
    tech_data['rsi'] = rsi
    tech_data['cci'] = cci
    tech_data['macd_hist'] = macd_hist
    
    long_score += long_momentum_bonus
    short_score += short_momentum_bonus

    # ----------------------------------------
    # 3. Structural/Pivot Point Confirmation
    # ----------------------------------------
    pivot_bonus = 0.0
    
    # 構造的サポート/レジスタンス (S1/R1) に価格が近いか
    price_range = latest['high'] - latest['low']
    pivot_range_factor = price_range * 0.5 
    
    # ロングシグナルの場合、価格がS1に近いことを確認
    if abs(close_price - latest['S1']) < pivot_range_factor:
        long_score += 0.05
        pivot_bonus += 0.05
        
    # ショートシグナルの場合、価格がR1に近いことを確認
    if abs(close_price - latest['R1']) < pivot_range_factor:
        short_score += 0.05
        pivot_bonus += 0.05

    tech_data['structural_pivot_bonus'] = pivot_bonus
    
    # ----------------------------------------
    # 4. Long-Term Trend Reversal Penalty (長期トレンドとの逆張りペナルティ)
    # ----------------------------------------
    long_term_reversal_penalty = False
    long_term_reversal_penalty_value = 0.0
    
    if long_term_trend == 'Short' and (long_score > short_score or close_price < latest['R1']): # ロング逆張り
        long_score -= LONG_TERM_REVERSAL_PENALTY
        long_term_reversal_penalty = True
        long_term_reversal_penalty_value = LONG_TERM_REVERSAL_PENALTY
    elif long_term_trend == 'Long' and (short_score > long_score or close_price > latest['S1']): # ショート逆張り
        short_score -= LONG_TERM_REVERSAL_PENALTY
        long_term_reversal_penalty = True
        long_term_reversal_penalty_value = LONG_TERM_REVERSAL_PENALTY
        
    tech_data['long_term_trend'] = long_term_trend
    tech_data['long_term_reversal_penalty'] = long_term_reversal_penalty
    tech_data['long_term_reversal_penalty_value'] = long_term_reversal_penalty_value

    # ----------------------------------------
    # 5. Volume Confirmation (流動性/出来高確証)
    # ----------------------------------------
    volume_confirmation_bonus = 0.0
    
    if latest['volume_ratio'] >= VOLUME_CONFIRMATION_MULTIPLIER:
        # 大口参加や明確なブレイクアウトの可能性がある場合にボーナス
        long_score += 0.05 
        short_score += 0.05
        volume_confirmation_bonus = 0.05

    tech_data['volume_confirmation_bonus'] = volume_confirmation_bonus
    tech_data['volume_ratio'] = latest['volume_ratio']

    # ----------------------------------------
    # 6. VWAP Consistency (価格とボリュームの整合性)
    # ----------------------------------------
    vwap_consistent = False
    if close_price > latest['vwap_proxy']:
        long_score += 0.03
        vwap_consistent = True
    elif close_price < latest['vwap_proxy']:
        short_score += 0.03
        vwap_consistent = True
        
    tech_data['vwap_consistent'] = vwap_consistent
    
    # ----------------------------------------
    # 7. Macro Context / Bias Filters (FR & Dominance)
    # ----------------------------------------
    funding_rate_bonus_value = 0.0
    dominance_bias_bonus_value = 0.0
    symbol = df.name # Pandas Seriesのname属性にシンボルを格納していると仮定

    # Funding Rate Bias
    # マイナスFR（ショート過密）ならロングにボーナス / プラスFR（ロング過密）ならショートにボーナス
    tech_data['funding_rate_value'] = funding_rate
    if funding_rate < -FUNDING_RATE_THRESHOLD:
        long_score += FUNDING_RATE_BONUS_PENALTY
        funding_rate_bonus_value = FUNDING_RATE_BONUS_PENALTY
    elif funding_rate > FUNDING_RATE_THRESHOLD:
        short_score += FUNDING_RATE_BONUS_PENALTY
        funding_rate_bonus_value = FUNDING_RATE_BONUS_PENALTY
    # 中立FRだが、一方のシグナルが非常に強い場合は、過密側をペナルティ
    elif funding_rate > 0.0003 and short_score > long_score:
        short_score -= FUNDING_RATE_BONUS_PENALTY / 2
        funding_rate_bonus_value = -FUNDING_RATE_BONUS_PENALTY / 2
    elif funding_rate < -0.0003 and long_score > short_score:
        long_score -= FUNDING_RATE_BONUS_PENALTY / 2
        funding_rate_bonus_value = -FUNDING_RATE_BONUS_PENALTY / 2
        
    tech_data['funding_rate_bonus_value'] = funding_rate_bonus_value

    # Dominance Bias Filter (BTC-USDTは対象外)
    dominance_trend = macro_context.get('dominance_trend', 'Neutral')
    tech_data['dominance_trend'] = dominance_trend
    
    if symbol != 'BTC-USDT':
        if dominance_trend == 'Down':
            # ドミナンス減少（アルトへの資金流入傾向）-> ロングにボーナス
            long_score += DOMINANCE_BIAS_BONUS_PENALTY
            dominance_bias_bonus_value = DOMINANCE_BIAS_BONUS_PENALTY
        elif dominance_trend == 'Up':
            # ドミナンス増加（アルトからの資金流出傾向）-> ロング/ショートにペナルティ
            long_score -= DOMINANCE_BIAS_BONUS_PENALTY / 2 
            short_score -= DOMINANCE_BIAS_BONUS_PENALTY / 2
            dominance_bias_bonus_value = -DOMINANCE_BIAS_BONUS_PENALTY / 2
            
    tech_data['dominance_bias_bonus_value'] = dominance_bias_bonus_value


    # ----------------------------------------
    # 8. Final Score and Decision
    # ----------------------------------------
    
    # スコアの正規化 (0.40未満にはしない)
    long_score = max(BASE_SCORE, long_score)
    short_score = max(BASE_SCORE, short_score)
    
    # 最終的なシグナル判定
    if long_score >= SIGNAL_THRESHOLD and long_score > short_score:
        return "Long", long_score, tech_data
    elif short_score >= SIGNAL_THRESHOLD and short_score > long_score:
        return "Short", short_score, tech_data
    elif long_score > short_score:
        return "WeakLong", long_score, tech_data
    elif short_score > short_score:
        return "WeakShort", short_score, tech_data
    else:
        return "Neutral", long_score, tech_data


def perform_analysis_and_signal_generation(symbol: str, timeframe: str, macro_context: Dict) -> Dict:
    """
    指定された時間軸とシンボルで分析を実行し、取引シグナルを生成する。
    Dynamic Trailing Stop (DTS)とStructural SLのロジックを統合。
    """
    try:
        # 1. データ取得
        ohlcv, status, _ = await fetch_ohlcv_with_fallback(CCXT_CLIENT_NAME, symbol, timeframe)
        if status != "Success":
            return {'symbol': symbol, 'timeframe': timeframe, 'side': status, 'score': 0.0}

        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df = df.set_index('timestamp')
        df['macd_hist_1'] = ta.macd(df['close'].shift(1))['MACDh_12_26_9']
        df.name = symbol # スコアリング関数内で利用するためシンボル名を格納

        # 2. テクニカル指標の計算
        df = calculate_technical_indicators(df, timeframe)
        
        # データが不十分な場合
        if df.empty or len(df) < 50: 
             return {'symbol': symbol, 'timeframe': timeframe, 'side': "DataShortage", 'score': 0.0}
        
        latest = df.iloc[-1]
        
        # 3. 長期トレンドの判定 (4h足専用 or 全ての足で利用)
        long_term_trend = get_long_term_trend(df)
        
        # 4. 資金調達率の取得
        funding_rate = macro_context.get('funding_rates', {}).get(symbol, 0.0)

        # 5. シグナルスコアの計算
        side, score, tech_data = calculate_trade_score(df, timeframe, long_term_trend, funding_rate, macro_context)
        
        tech_data['long_term_trend'] = long_term_trend
        tech_data['atr_value'] = latest['atr']
        
        # 6. リスク管理パラメーターの計算 (Entry/SL/TP)
        entry, sl, tp1, rr_ratio, entry_type, structural_sl_used = 0.0, 0.0, 0.0, 0.0, 'N/A', False
        
        current_price = latest['close']
        atr_sl_width = latest['atr'] * ATR_TRAIL_MULTIPLIER

        if side in ["Long", "WeakLong"]:
            # エントリー：現在の価格より低いS1またはS2 (Mean Reversion)
            entry_type = 'Limit'
            entry = latest['S1'] if latest['S1'] < current_price else current_price 
            
            # SL: S1 (構造的SL) またはATR SL
            potential_sl_structural = latest['S1'] - (0.5 * latest['atr']) # S1に0.5 ATRのバッファを追加
            
            # 構造的SLがATR幅より優れている場合（リスクが小さい場合）
            if potential_sl_structural > (current_price - atr_sl_width):
                 sl = potential_sl_structural
                 structural_sl_used = True
            else:
                 sl = current_price - atr_sl_width
                 
            # TP (DTS戦略のため、ここではRRR: 1:DTS_RRR_DISPLAYで計算した遠い目標値)
            risk_width = entry - sl # 実際のリスク幅
            tp1 = entry + (risk_width * DTS_RRR_DISPLAY)
            rr_ratio = (tp1 - entry) / risk_width if risk_width > 0 else 0.0
            
            # EntryがSLを下回る異常ケースの修正
            if entry < sl: entry = current_price # EntryをCurrent Priceに修正
            if sl >= entry: sl = current_price - atr_sl_width # SLをATR幅で強制設定

        elif side in ["Short", "WeakShort"]:
            # エントリー：現在の価格より高いR1またはR2
            entry_type = 'Limit'
            entry = latest['R1'] if latest['R1'] > current_price else current_price
            
            # SL: R1 (構造的SL) またはATR SL
            potential_sl_structural = latest['R1'] + (0.5 * latest['atr']) # R1に0.5 ATRのバッファを追加

            # 構造的SLがATR幅より優れている場合（リスクが小さい場合）
            if potential_sl_structural < (current_price + atr_sl_width):
                sl = potential_sl_structural
                structural_sl_used = True
            else:
                sl = current_price + atr_sl_width
                
            # TP (DTS戦略のため、ここではRRR: 1:DTS_RRR_DISPLAYで計算した遠い目標値)
            risk_width = sl - entry # 実際のリスク幅
            tp1 = entry - (risk_width * DTS_RRR_DISPLAY)
            rr_ratio = (entry - tp1) / risk_width if risk_width > 0 else 0.0
            
            # EntryがSLを上回る異常ケースの修正
            if entry > sl: entry = current_price # EntryをCurrent Priceに修正
            if sl <= entry: sl = current_price + atr_sl_width # SLをATR幅で強制設定
            
        tech_data['structural_sl_used'] = structural_sl_used

        # 7. 結果の統合
        return {
            'symbol': symbol,
            'timeframe': timeframe,
            'side': side,
            'score': score,
            'price': current_price,
            'entry': entry,
            'sl': sl,
            'tp1': tp1,
            'rr_ratio': rr_ratio,
            'entry_type': entry_type,
            'regime': tech_data.get('regime'),
            'tech_data': tech_data,
            'macro_context': macro_context
        }

    except Exception as e:
        logging.error(f"分析中にエラーが発生しました ({symbol}, {timeframe}): {e}")
        return {'symbol': symbol, 'timeframe': timeframe, 'side': "ExchangeError", 'score': 0.0}


# ====================================================================================
# MAIN LOOP
# ====================================================================================

async def main_loop():
    """メインの非同期ループ。市場を監視し、シグナルを生成・通知する。"""
    global LAST_SUCCESS_TIME, GLOBAL_MACRO_CONTEXT, TRADE_NOTIFIED_SYMBOLS
    
    # 実行前にCCXTクライアントを初期化
    await initialize_ccxt_client()

    while True:
        try:
            logging.info("--- 🔄 新しい分析サイクルを開始 ---")
            
            current_time = time.time()
            
            # 1. クールダウン中のシグナルをクリア (クールダウン撤廃のため、このステップはスキップ)
            # TRADE_NOTIFIED_SYMBOLS は通知チェックに使用されなくなりました。

            # 2. マクロコンテキストの取得
            if current_time - LAST_UPDATE_TIME > 60 * 60: # 1時間ごとにマクロ情報を更新
                macro_context = await get_crypto_macro_context()
                LAST_UPDATE_TIME = current_time
            else:
                macro_context = GLOBAL_MACRO_CONTEXT
            
            # 3. 資金調達率を事前に取得 (全監視シンボル)
            funding_rates_tasks = [fetch_funding_rate(s) for s in CURRENT_MONITOR_SYMBOLS]
            funding_rates = await asyncio.gather(*funding_rates_tasks, return_exceptions=True)
            
            fr_dict = {}
            for i, symbol in enumerate(CURRENT_MONITOR_SYMBOLS):
                if not isinstance(funding_rates[i], Exception):
                    fr_dict[symbol] = funding_rates[i]
            
            macro_context['funding_rates'] = fr_dict
            
            # 4. 並行処理で分析を実行 (15m, 1h, 4hの3つの時間軸)
            analysis_tasks = []
            timeframes = ['15m', '1h', '4h']
            
            for symbol in CURRENT_MONITOR_SYMBOLS:
                # クールダウンチェックを削除し、常に分析を実行します
                for tf in timeframes:
                    # 1シンボルあたり0.5秒のレート制限を考慮して、タスクを生成
                    analysis_tasks.append(perform_analysis_and_signal_generation(symbol, tf, macro_context))
                    await asyncio.sleep(REQUEST_DELAY_PER_SYMBOL / len(timeframes))
                
            all_signals = await asyncio.gather(*analysis_tasks, return_exceptions=True)
            
            # 5. シグナル結果を集計・フィルタリング
            signal_map: Dict[str, List[Dict]] = {}
            for result in all_signals:
                if isinstance(result, dict) and 'symbol' in result:
                    symbol = result['symbol']
                    if symbol not in signal_map:
                        signal_map[symbol] = []
                    signal_map[symbol].append(result)
                elif isinstance(result, Exception):
                    logging.error(f"並行タスクでエラーが発生: {result}")


            # 6. ベストシグナルの特定と通知
            # 各シンボルで最もスコアの高いシグナル（3つの時間軸のうち）を抽出
            best_signals_per_symbol = []
            
            for symbol, signals in signal_map.items():
                high_score_signals = [s for s in signals if s.get('score', 0.5) >= SIGNAL_THRESHOLD and s.get('side') in ["Long", "Short"]]
                
                if high_score_signals:
                    # RRR、スコア、ADX、ATRの順にソートして、最も良いシグナルを特定
                    best_signal = max(
                        high_score_signals, 
                        key=lambda s: (
                            s.get('score', 0.5), 
                            s.get('rr_ratio', 0.0), 
                            s.get('tech_data', {}).get('adx', 0.0), 
                            -s.get('tech_data', {}).get('atr_value', 1.0)
                        )
                    )
                    # 統合メッセージ生成のために、全てのシグナルを保持
                    best_signal['all_signals'] = signals
                    best_signals_per_symbol.append(best_signal)

            # 全シンボルからスコアの高い順にTOP Nを抽出
            final_signals = sorted(
                best_signals_per_symbol, 
                key=lambda s: s['score'], 
                reverse=True
            )[:TOP_SIGNAL_COUNT]
            
            LAST_ANALYSIS_SIGNALS = final_signals
            notification_count = 0

            for rank, signal in enumerate(final_signals, 1):
                symbol = signal['symbol']
                
                # Telegram通知を送信 (クールダウンチェックなし)
                telegram_message = format_integrated_analysis_message(symbol, signal['all_signals'], rank)
                if telegram_message:
                    is_sent = send_telegram_html(telegram_message)
                    if is_sent:
                        # クールダウンがないため、TRADE_NOTIFIED_SYMBOLSへの記録は不要
                        notification_count += 1
                        
            # 7. 出来高による監視シンボルの更新 (24時間ごと)
            if (current_time - LAST_SUCCESS_TIME) > 60 * 60 * 24:
                 await update_symbols_by_volume()
                
            LAST_SUCCESS_TIME = current_time
            logging.info(f"--- ✅ 分析サイクルを完了しました (通知数: {notification_count}件 / 監視シンボル数: {len(CURRENT_MONITOR_SYMBOLS)}件) ---")
            
            # 8. 次のループまで待機
            await asyncio.sleep(LOOP_INTERVAL)

        except (ccxt.NetworkError, ccxt.ExchangeError) as e:
            error_name = type(e).__name__
            logging.error(f"メインループでCCXT関連のエラー: {error_name}")
            if EXCHANGE_CLIENT:
                 await EXCHANGE_CLIENT.close()
            await initialize_ccxt_client() # クライアントを再初期化
            await asyncio.sleep(60) # 60秒待機
        
        except Exception as e:
            error_name = type(e).__name__
            
            # CCXTクライアントが閉じている可能性があるため再初期化を試行
            if 'exchange' in str(e).lower() and EXCHANGE_CLIENT:
                 await EXCHANGE_CLIENT.close()
                 await initialize_ccxt_client() 
            
            logging.error(f"メインループで致命的なエラー: {error_name}")
            await asyncio.sleep(60)


# ====================================================================================
# FASTAPI SETUP
# ====================================================================================

# バージョンを v19.0.4 に更新
app = FastAPI(title="Apex BOT API", version="v19.0.4 - Cooldown Removed")

@app.on_event("startup")
async def startup_event():
    logging.info("🚀 Apex BOT v19.0.4 Startup initializing...") # バージョン更新
    # メインループを非同期タスクとして開始
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
        "bot_version": "v19.0.4 - Cooldown Removed", # バージョン更新
        "last_success_time_utc": datetime.fromtimestamp(LAST_SUCCESS_TIME, tz=timezone.utc).isoformat() if LAST_SUCCESS_TIME else "N/A",
        "current_client": CCXT_CLIENT_NAME,
        "monitoring_symbols": len(CURRENT_MONITOR_SYMBOLS),
        "last_signals_count": len(LAST_ANALYSIS_SIGNALS)
    }
    return JSONResponse(content=status_msg)

@app.head("/")
@app.get("/")
def home_view():
    return JSONResponse(content={"message": "Apex BOT is running on v19.0.4"})
