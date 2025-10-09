# ====================================================================================
# Apex BOT v16.0.2 - Heatmap Feature Add (Structural SL Buffer Fix Base)
# - NEW: BTC/USDT 1時間足の価格帯出来高ヒートマップを生成し、Telegramに通知する機能を追加
# - FIX: 構造的SL (S1/R1) を使用する際に、エントリーポイントとの一致を避けるため、SLに 0.5 * ATR のバッファを追加
# - BTCドミナンスの増減トレンドを判定し、Altcoinのシグナルスコアに反映 (+/- 0.05点)
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
# --- ヒートマップ機能の追加 ---
import matplotlib.pyplot as plt
import seaborn as sns 
# ------------------------------

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
# --- ヒートマップ機能の追加 ---
HEATMAP_INTERVAL = 60 * 60 * 1  # 1時間 (秒)
LAST_HEATMAP_TIME: float = 0.0
# ------------------------------

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

# --- ヒートマップ機能の追加 ---
def send_telegram_photo(caption: str, photo_path: str) -> bool:
    """Telegramに画像ファイルとキャプションを送信する"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    
    if not os.path.exists(photo_path):
        logging.error(f"Telegram Photo Error: 画像ファイルが見つかりません: {photo_path}")
        return False
        
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'caption': caption,
        'parse_mode': 'HTML'
    }
    
    # 画像ファイルをバイナリで開く
    files = {
        'photo': (os.path.basename(photo_path), open(photo_path, 'rb'), 'image/png')
    }
    
    try:
        # dataではなくfilesとして画像を送信
        response = requests.post(url, data=payload, files=files)
        response.raise_for_status()
        logging.info("Telegramにヒートマップ画像を送信しました。")
        return True
    except requests.exceptions.HTTPError as e:
        logging.error(f"Telegram Photo HTTP Error: {e.response.text if e.response else 'N/A'}")
        return False
    except requests.exceptions.RequestException as e:
        logging.error(f"Telegram Photo Request Error: {e}")
        return False
    finally:
        # ファイルを閉じて削除 (メモリ/ディスクのクリーンアップ)
        if 'photo' in files and files['photo'][1]:
            files['photo'][1].close()
        try:
            os.remove(photo_path)
            logging.info(f"一時ファイル {photo_path} を削除しました。")
        except OSError as e:
            logging.error(f"一時ファイルの削除に失敗しました: {e}")
            pass
# ------------------------------

def get_estimated_win_rate(score: float, timeframe: str) -> float:
    """スコアと時間軸に基づき推定勝率を算出する (0.0 - 1.0 スケールで計算)"""
    adjusted_rate = 0.50 + (score - 0.50) * 1.45 
    return max(0.40, min(0.85, adjusted_rate))


def format_integrated_analysis_message(symbol: str, signals: List[Dict], rank: int) -> str:
    """
    3つの時間軸の分析結果を統合し、ログメッセージの形式に整形する (v16.0.1対応)
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
        f"| ⚙️ **BOT Ver** | **v16.0.2** - Heatmap Feature Add |\n" # バージョン更新
        f"==================================\n"
        f"\n<pre>※ Limit注文は、価格が指定水準に到達した際のみ約定します。DTS戦略では、価格が有利な方向に動いた場合、SLが自動的に追跡され利益を最大化します。</pre>"
    )

    return header + trade_plan + analysis_detail + footer


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
            logging.info(f"✅ 出来高トップ {TOP_SYMBOL_LIMIT} 銘柄を動的に更新しました。")
            
            # DEFAULT_SYMBOLSに含まれていないものがあれば追加でログに出力
            default_symbols_okx = [convert_symbol_to_okx_swap(s) for s in DEFAULT_SYMBOLS]
            newly_added = [s for s in new_monitor_symbols if s not in default_symbols_okx]
            if newly_added:
                logging.info(f"🆕 新規追加銘柄 (Default外): {', '.join(newly_added)}")
        else:
            CURRENT_MONITOR_SYMBOLS = LAST_SUCCESSFUL_MONITOR_SYMBOLS
            logging.warning("⚠️ 出来高データを取得できませんでした。前回成功したリストを使用します。")
            
    except Exception as e:
        logging.error(f"出来高による銘柄更新中にエラーが発生しました: {e}")
        CURRENT_MONITOR_SYMBOLS = LAST_SUCCESSFUL_MONITOR_SYMBOLS
        logging.warning("⚠️ 出来高データ取得エラー。前回成功したリストにフォールバックします。")


async def fetch_ohlcv_with_fallback(client_name: str, symbol: str, timeframe: str, limit: Optional[int] = None) -> Tuple[List[List[float]], str, str]:
    """CCXTを使用してOHLCVデータを取得し、エラー発生時にフォールバックする"""
    global EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT:
        return [], "ExchangeError", client_name

    try:
        # limitが指定されていればそれを使用、なければ既存のREQUIRED_OHLCV_LIMITSを使用
        fetch_limit = limit if limit is not None else REQUIRED_OHLCV_LIMITS.get(timeframe, 100)
        
        # 1. SWAP (無期限先物) データ取得を試みる
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=fetch_limit, params={'instType': 'SWAP'})
        
        if not ohlcv or len(ohlcv) < 30:
            return [], "DataShortage", client_name
        
        return ohlcv, "Success", client_name
        
    except ccxt.NetworkError as e:
        return [], "ExchangeError", client_name
        
    except ccxt.ExchangeError as e:
        # SWAPが見つからない場合はSPOT (現物) にフォールバック
        if 'market symbol' in str(e) or 'not found' in str(e):
            spot_symbol = symbol.replace('-', '/')
            try:
                # 2. SPOT (現物) データ取得を試みる
                ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(spot_symbol, timeframe, limit=fetch_limit, params={'instType': 'SPOT'})
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
    # fetch_ohlcv_with_fallback の引数を修正
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
        df_btc.dropna(subset=['close'], inplace=True)
        
        # 50期間SMA (長期トレンド)
        df_btc['sma_long'] = df_btc['close'].rolling(window=LONG_TERM_SMA_LENGTH).mean()
        last_btc_close = df_btc['close'].iloc[-1]
        last_btc_sma = df_btc['sma_long'].iloc[-1]
        
        # 1.5日分 (9本) の4h足の価格変化率
        btc_change = (df_btc['close'].iloc[-1] - df_btc['open'].iloc[-9]) / df_btc['open'].iloc[-9] if len(df_btc) >= 9 else 0.0
        
        if last_btc_close > last_btc_sma:
            btc_trend = 1 # Long
        elif last_btc_close < last_btc_sma:
            btc_trend = -1 # Short
            
    if status_eth == "Success":
        df_eth = pd.DataFrame(eth_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df_eth['close'] = pd.to_numeric(df_eth['close'], errors='coerce').astype('float64')
        df_eth.dropna(subset=['close'], inplace=True)
        
        # 1.5日分 (9本) の4h足の価格変化率
        eth_change = (df_eth['close'].iloc[-1] - df_eth['open'].iloc[-9]) / df_eth['open'].iloc[-9] if len(df_eth) >= 9 else 0.0

    # 2. Fear & Greed Index (FGI) Proxyの算出
    # (BTCとETHの4h足の価格変化率の平均をセンチメントプロキシとして使用)
    fgi_proxy = (btc_change + eth_change) / 2.0
    
    # 3. BTC Dominance Bias Trend (ドミナンスの増減トレンド) の判定
    # BTCとETHの価格変化率の差分を指標とする
    # positive = BTCがETHより強い (BTC Dominanceの上昇トレンド、Altcoinに不利)
    # negative = ETHがBTCより強い (BTC Dominanceの下降トレンド、Altcoinに有利)
    dominance_diff = btc_change - eth_change
    dominance_trend = 'Neutral'
    
    if dominance_diff > 0.005: # BTCの方が0.5%以上強い
        dominance_trend = 'Up' 
    elif dominance_diff < -0.005: # ETHの方が0.5%以上強い
        dominance_trend = 'Down' 
        
    logging.info(f"🌍 マクロコンテキスト更新: BTC Trend({btc_trend}), ETH Change({eth_change*100:.2f}%), FGI Proxy({fgi_proxy*100:.2f}%), Dominance Trend({dominance_trend})")

    return {
        "btc_long_term_trend": btc_trend,
        "eth_price_change": eth_change,
        "sentiment_fgi_proxy": fgi_proxy,
        "dominance_trend": dominance_trend
    }

# --- ヒートマップ機能の追加 ---
async def create_btc_heatmap_and_notify():
    """BTC/USDTの1時間足ヒートマップを生成し、反発しやすい箇所を通知する (1時間ごと)"""
    global LAST_HEATMAP_TIME
    
    # 実行時間チェック
    current_time = time.time()
    if current_time - LAST_HEATMAP_TIME < HEATMAP_INTERVAL:
        return
    
    LAST_HEATMAP_TIME = current_time # 実行時間を更新
    
    SYMBOL = 'BTC-USDT'
    TIMEFRAME = '1h'
    LIMIT = 200 # 過去200時間分のデータを取得

    # 1. データ取得
    # limit=LIMIT を引数として渡す
    ohlcv, status, _ = await fetch_ohlcv_with_fallback(CCXT_CLIENT_NAME, SYMBOL, TIMEFRAME, limit=LIMIT)
    
    if status != "Success":
        logging.warning(f"BTC/USDT ヒートマップ用データ取得失敗: {status}")
        return

    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['close'] = pd.to_numeric(df['close'], errors='coerce')
    df['volume'] = pd.to_numeric(df['volume'], errors='coerce')
    df.dropna(subset=['close', 'volume'], inplace=True)

    df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms').dt.tz_localize('UTC').dt.tz_convert(JST)
    df.set_index('datetime', inplace=True)
    
    # 2. ヒートマップの準備 (Price-Volume Distribution)
    # 価格を一定のビンに区切り、各ビン内の出来高を集計する (価格帯出来高: Volume Profile の簡易版)
    
    # 価格レンジを決定
    min_price = df['low'].min()
    max_price = df['high'].max()
    price_range = max_price - min_price
    
    # ヒートマップの解像度を決定 (50ビン)
    num_bins = 50
    # 価格レンジの上下にバッファを持たせる
    bins = np.linspace(min_price - price_range * 0.005, max_price + price_range * 0.005, num_bins + 1)
    
    # 出来高を重みとして、終値のヒストグラムを作成
    hist, bin_edges = np.histogram(df['close'], bins=bins, weights=df['volume'])
    
    # 3. 反発しやすい箇所の特定 (主要な出来高ノード)
    # 出来高密度が平均の1.5倍以上の価格帯を抽出
    avg_hist = np.mean(hist)
    reversal_points = []
    
    # 現在価格を取得
    current_price = df['close'].iloc[-1]
    
    for i in range(len(hist)):
        # 出来高が大きく、かつ現在価格の ±5% 以内にあるノードのみを対象とする
        price_level = (bin_edges[i] + bin_edges[i+1]) / 2
        
        if hist[i] > avg_hist * 1.5 and abs(price_level - current_price) / current_price < 0.05: 
            reversal_points.append({
                'price': price_level, 
                'density': hist[i]
            })
            
    # 出来高密度の高い順にソートして、上位3つに限定
    reversal_points = sorted(reversal_points, key=lambda x: x['density'], reverse=True)[:3]

    
    # 4. ヒートマップの描画
    file_path = f'btc_usdt_heatmap_{current_time:.0f}.png'

    plt.style.use('dark_background') 
    fig, ax = plt.subplots(figsize=(8, 12))
    
    # ヒストグラムの棒グラフを横向きに描画
    # histが全て0の場合のZeroDivisionErrorを回避
    if np.max(hist) > 0:
        normalized_hist = (hist - np.min(hist)) / (np.max(hist) - np.min(hist))
    else:
        normalized_hist = np.zeros_like(hist)
        
    bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2
    
    # 色を密度に応じて変化させる
    ax.barh(bin_centers, hist, height=(bin_edges[1]-bin_edges[0]) * 0.9, # バーの間に隙間を空ける
            color=sns.color_palette("magma", as_cmap=True)(normalized_hist), 
            edgecolor='none')

    # 反発候補価格をプロット
    for rp in reversal_points:
        price_str = format_price_utility(rp['price'], SYMBOL)
        
        ax.axhline(rp['price'], color='lime', linestyle='--', linewidth=1, alpha=0.7)
        # 価格ラベルを画像に描画
        ax.text(ax.get_xlim()[1] * 0.95, rp['price'], 
                f"S/R 候補: {price_str}", 
                color='lime', fontsize=10, 
                verticalalignment='center', horizontalalignment='right', 
                bbox=dict(facecolor='black', alpha=0.5, edgecolor='none', boxstyle='round,pad=0.3'))
    
    # 現在価格をプロット
    current_price_str = format_price_utility(current_price, SYMBOL)
    ax.axhline(current_price, color='red', linestyle='-', linewidth=2, 
               label=f"現在価格: {current_price_str}")
    
    ax.set_title(f"BTC/USDT 1h Price-Volume Distribution Heatmap\n(過去 {LIMIT} 時間 - {datetime.now(JST).strftime('%Y/%m/%d %H:%M:%S JST')})", fontsize=14)
    ax.set_xlabel("出来高密度", fontsize=12)
    ax.set_ylabel("価格 (USDT)", fontsize=12)
    ax.legend(loc='lower left', frameon=True, facecolor='black', edgecolor='white')
    ax.grid(axis='y', linestyle=':', alpha=0.3)
    
    # y軸の価格表示を整形
    from matplotlib.ticker import FuncFormatter
    formatter = FuncFormatter(lambda x, pos: format_price_utility(x, SYMBOL))
    ax.yaxis.set_major_formatter(formatter)
    
    plt.tight_layout()
    plt.savefig(file_path)
    plt.close(fig) # メモリ解放
    
    # 5. 通知メッセージの作成
    if reversal_points:
        rp_message = "\n".join([
            f"   - **{format_price_utility(rp['price'], SYMBOL)}**: 出来高集中ノード"
            for rp in reversal_points
        ])
        
        caption = (
            f"🔥 **BTC/USDT 1時間ヒートマップ分析** (最終価格: ${current_price_str})\n\n"
            f"**💡 反発となりやすい箇所 (主要な出来高集中価格帯)**:\n"
            f"{rp_message}\n\n"
            f"これらの価格帯は、強いサポートまたはレジスタンスとして機能する可能性が高いです。"
        )
    else:
        caption = (
            f"🟡 **BTC/USDT 1時間ヒートマップ分析** (最終価格: ${current_price_str})\n\n"
            f"現在の価格帯には、目立った出来高の集中ノードは見られません。\n"
            f"画像は過去200時間分の出来高密度を示しています。"
        )
        
    # 6. Telegramで画像とメッセージを送信
    send_telegram_photo(caption=caption, photo_path=file_path)
# ------------------------------

# ====================================================================================
# TECHNICAL ANALYSIS & SCORING LOGIC
# ====================================================================================

def calculate_pivot_points(df: pd.DataFrame) -> pd.DataFrame:
    """
    OHLCVデータフレームにフィボナッチピボットポイントを計算して追加する。
    R1/S1/R3/S3のみを使用。
    """
    
    if df.empty or len(df) < 2:
        df['PP'] = np.nan
        df['R1'] = np.nan
        df['S1'] = np.nan
        df['R2'] = np.nan
        df['S2'] = np.nan
        df['R3'] = np.nan
        df['S3'] = np.nan
        return df
        
    # 最新のローソク足は計算に使用せず、その1つ前の足を使う (Prev Day/Period High/Low/Close)
    prev_h = df['high'].shift(1)
    prev_l = df['low'].shift(1)
    prev_c = df['close'].shift(1)
    
    # Pivot Point (PP)
    df['PP'] = (prev_h + prev_l + prev_c) / 3
    
    # Range
    R = prev_h - prev_l
    
    # Fibonacci Levels
    df['R1'] = df['PP'] + (0.382 * R)
    df['S1'] = df['PP'] - (0.382 * R)
    
    df['R2'] = df['PP'] + (0.618 * R)
    df['S2'] = df['PP'] - (0.618 * R)

    df['R3'] = df['PP'] + (1.0 * R)
    df['S3'] = df['PP'] - (1.0 * R)
    
    return df

def analyze_single_timeframe(df: pd.DataFrame, timeframe: str, symbol: str, macro_context: Dict) -> Dict:
    """
    単一の時間軸 (timeframe) に基づくテクニカル分析とシグナルスコアリング
    """
    
    # 1. 基本チェック
    if df.empty or len(df) < 50:
        return {'side': 'DataShortage', 'score': 0.0, 'timeframe': timeframe, 'symbol': symbol, 'macro_context': macro_context}

    # 2. テクニカル指標の計算
    
    # SMA (長期トレンド判定用 - 50期間)
    df['sma_long'] = df['close'].rolling(window=LONG_TERM_SMA_LENGTH).mean()
    
    # ATR (ボラティリティ、SL/TP計算用)
    df.ta.atr(append=True, length=14)
    atr_col = df.columns[df.columns.str.startswith('ATR_14')][0]
    
    # RSI (モメンタム)
    df.ta.rsi(append=True, length=14)
    rsi_col = df.columns[df.columns.str.startswith('RSI_14')][0]

    # MACD (モメンタムの方向/クロス)
    df.ta.macd(append=True, fast=12, slow=26, signal=9)
    macd_hist_col = df.columns[df.columns.str.endswith('HIST')][0]
    
    # ADX (トレンドの強さ/Regime)
    df.ta.adx(append=True, length=14)
    adx_col = df.columns[df.columns.str.startswith('ADX_14')][0]
    
    # STOCHRSI (買われすぎ/売られすぎの過熱感 - ペナルティフィルター用)
    df.ta.stochrsi(append=True)
    stoch_k_col = df.columns[df.columns.str.startswith('STOCHRSIk')][0]
    stoch_d_col = df.columns[df.columns.str.startswith('STOCHRSId')][0]

    # CCI (トレンド確認)
    df.ta.cci(append=True)
    cci_col = df.columns[df.columns.str.startswith('CCI')][0]

    # VWAP (出来高加重平均価格)
    df['VWAP'] = df.ta.vwap(anchor='D', append=False) # デイリーVWAP

    # Bollinger Bands (ボラティリティフィルター用)
    df.ta.bbands(append=True, length=20, std=2)
    bbp_col = df.columns[df.columns.str.startswith('BBP_20')][0]
    
    # Pivot Points (構造的サポート/レジスタンス)
    df = calculate_pivot_points(df.copy())
    
    # 最後のローソク足のデータを取得 (シグナル判定に使用)
    # NaNチェックを強化
    last_row = df.iloc[-1]
    prev_row = df.iloc[-2] if len(df) >= 2 else last_row # 2つ前のローソク足

    # NaNの場合のフォールバック値を定義
    fallback_nan_float = np.nan

    # テクニカル指標の値を取得 (NaNを許容)
    rsi_val = last_row.get(rsi_col, fallback_nan_float)
    macd_hist_val = last_row.get(macd_hist_col, fallback_nan_float)
    adx_val = last_row.get(adx_col, fallback_nan_float)
    atr_val = last_row.get(atr_col, fallback_nan_float)
    stoch_k_val = last_row.get(stoch_k_col, fallback_nan_float)
    stoch_d_val = last_row.get(stoch_d_col, fallback_nan_float)
    cci_val = last_row.get(cci_col, fallback_nan_float)
    bbp_val = last_row.get(bbp_col, fallback_nan_float)
    vwap_val = last_row.get('VWAP', fallback_nan_float)
    
    # 3. Regime (相場環境) の判定
    adx_is_trend = adx_val >= ADX_TREND_THRESHOLD
    regime = 'Trend' if adx_is_trend else 'Range'
    
    # 4. ベーススコアリング (RSIとMACDに基づく)
    side = 'Neutral'
    score = BASE_SCORE
    
    # ----- Long Signal Base Score -----
    if rsi_val <= RSI_OVERSOLD and macd_hist_val > 0 and macd_hist_val > prev_row.get(macd_hist_col, fallback_nan_float) :
        side = 'ロング'
        score += 0.20 # ベースのシグナル点

    # RSIがRSI_MOMENTUM_LOWを下回り、MACDヒストグラムが上向きに転じた
    elif rsi_val < RSI_MOMENTUM_LOW and macd_hist_val > 0 and prev_row.get(macd_hist_col, fallback_nan_float) <= 0:
        side = 'ロング'
        score += 0.15

    # ----- Short Signal Base Score -----
    elif rsi_val >= RSI_OVERBOUGHT and macd_hist_val < 0 and macd_hist_val < prev_row.get(macd_hist_col, fallback_nan_float):
        side = 'ショート'
        score += 0.20

    # RSIがRSI_MOMENTUM_HIGHを上回り、MACDヒストグラムが下向きに転じた
    elif rsi_val > RSI_MOMENTUM_HIGH and macd_hist_val < 0 and prev_row.get(macd_hist_col, fallback_nan_float) >= 0:
        side = 'ショート'
        score += 0.15

    # 5. スコア調整 (フィルターとボーナス)
    
    long_term_reversal_penalty = False
    macd_cross_penalty = False
    stoch_filter_penalty = 0.0
    volume_confirmation_bonus = 0.0
    funding_rate_bonus_value = 0.0
    structural_pivot_bonus = 0.0
    dominance_bias_bonus_value = 0.0
    
    current_price = last_row.get('close', fallback_nan_float)
    current_volume = last_row.get('volume', fallback_nan_float)
    
    if np.isnan(current_price) or np.isnan(current_volume):
        return {'side': 'DataShortage', 'score': 0.0, 'timeframe': timeframe, 'symbol': symbol, 'macro_context': macro_context}

    if side != 'Neutral':
        
        # A. 長期トレンドとの逆行ペナルティ (4h足との一致性)
        # 1hと15m足にのみ適用
        if timeframe in ['15m', '1h']:
            long_term_trend = macro_context.get('btc_long_term_trend', 0)
            
            # Longシグナルだが、BTCの4hトレンドがShort ( -1 )
            if side == 'ロング' and long_term_trend < 0:
                score -= LONG_TERM_REVERSAL_PENALTY
                long_term_reversal_penalty = True
            
            # Shortシグナルだが、BTCの4hトレンドがLong ( 1 )
            elif side == 'ショート' and long_term_trend > 0:
                score -= LONG_TERM_REVERSAL_PENALTY
                long_term_reversal_penalty = True
                
        # B. MACDゼロクロス付近での逆行ペナルティ (モメンタムの確証)
        # MACD線とシグナル線のクロスがシグナルと逆行している場合、ペナルティ
        if np.isnan(macd_hist_val) or np.isnan(prev_row.get(macd_hist_col, fallback_nan_float)):
             # データがない場合はスキップ
             pass
        else:
            macd_line = last_row.get(macd_hist_col.replace('_HIST', ''), fallback_nan_float) - last_row.get(macd_hist_col.replace('_HIST', '_SIGNAL'), fallback_nan_float)
            prev_macd_line = prev_row.get(macd_hist_col.replace('_HIST', ''), fallback_nan_float) - prev_row.get(macd_hist_col.replace('_HIST', '_SIGNAL'), fallback_nan_float)

            # ロングシグナルだが、MACD線がシグナル線を下抜けている (モメンタムの失速/反転)
            if side == 'ロング' and macd_line < 0 and prev_macd_line >= 0:
                 score -= MACD_CROSS_PENALTY
                 macd_cross_penalty = True

            # ショートシグナルだが、MACD線がシグナル線を上抜けている (モメンタムの失速/反転)
            elif side == 'ショート' and macd_line > 0 and prev_macd_line <= 0:
                 score -= MACD_CROSS_PENALTY
                 macd_cross_penalty = True

        # C. STOCHRSI 過熱感ペナルティ (レンジ相場での騙し回避)
        # ADXがトレンド相場ではない時 (Regime=Range) に、過熱感がありすぎる場合にペナルティ
        if regime == 'Range' and not np.isnan(stoch_k_val) and not np.isnan(stoch_d_val):
            # ロングシグナルだが、STOCHRSIが買われすぎ水準 (80以上) にある
            if side == 'ロング' and stoch_k_val > 80 and stoch_d_val > 80:
                stoch_filter_penalty = 0.10
                score -= stoch_filter_penalty
            # ショートシグナルだが、STOCHRSIが売られすぎ水準 (20以下) にある
            elif side == 'ショート' and stoch_k_val < 20 and stoch_d_val < 20:
                stoch_filter_penalty = 0.10
                score -= stoch_filter_penalty
                
        # D. 出来高確証ボーナス
        # 直近の出来高が過去の平均出来高と比較してVOLUME_CONFIRMATION_MULTIPLIER倍以上の場合
        volume_ratio = 0.0
        if current_volume > 0 and len(df) >= 20:
            avg_volume = df['volume'].iloc[-20:-1].mean()
            if avg_volume > 0:
                volume_ratio = current_volume / avg_volume
                if volume_ratio >= VOLUME_CONFIRMATION_MULTIPLIER:
                    volume_confirmation_bonus = 0.10
                    score += volume_confirmation_bonus
        
        # E. 構造的サポート/レジスタンス (Pivot) ボーナス
        # LongシグナルのエントリーポイントがS1/S2/S3の近くにある
        # ShortシグナルのエントリーポイントがR1/R2/R3の近くにある
        # 距離の閾値として 1.0 * ATR を使用
        
        if not np.isnan(atr_val) and atr_val > 0:
            
            # 現在価格とPivotレベルとの絶対的な差
            pivot_levels = {
                'R1': last_row.get('R1', fallback_nan_float), 'S1': last_row.get('S1', fallback_nan_float),
                'R2': last_row.get('R2', fallback_nan_float), 'S2': last_row.get('S2', fallback_nan_float),
                'R3': last_row.get('R3', fallback_nan_float), 'S3': last_row.get('S3', fallback_nan_float)
            }
            
            atr_threshold = 1.0 * atr_val 
            
            if side == 'ロング':
                # S1, S2, S3 のいずれかに近いか
                for level in ['S1', 'S2', 'S3']:
                    level_price = pivot_levels[level]
                    if not np.isnan(level_price) and abs(current_price - level_price) <= atr_threshold:
                        structural_pivot_bonus = 0.10 
                        score += structural_pivot_bonus
                        break
                        
            elif side == 'ショート':
                # R1, R2, R3 のいずれかに近いか
                for level in ['R1', 'R2', 'R3']:
                    level_price = pivot_levels[level]
                    if not np.isnan(level_price) and abs(current_price - level_price) <= atr_threshold:
                        structural_pivot_bonus = 0.10
                        score += structural_pivot_bonus
                        break

        # F. Funding Rate Bias (Altcoinにのみ適用)
        # Longシグナルの場合: FRがマイナスで大きなペナルティが発生していない -> 良い
        # Shortシグナルの場合: FRがプラスで大きなペナルティが発生していない -> 良い
        if symbol != 'BTC-USDT':
            funding_rate = await fetch_funding_rate(symbol)
            
            if abs(funding_rate) >= FUNDING_RATE_THRESHOLD:
                if side == 'ロング':
                    if funding_rate < 0:
                        funding_rate_bonus_value = FUNDING_RATE_BONUS_PENALTY # マイナスFRでロングはボーナス
                        score += funding_rate_bonus_value
                    else:
                        funding_rate_bonus_value = -FUNDING_RATE_BONUS_PENALTY # プラスFRでロングはペナルティ (過密)
                        score += funding_rate_bonus_value
                        
                elif side == 'ショート':
                    if funding_rate > 0:
                        funding_rate_bonus_value = FUNDING_RATE_BONUS_PENALTY # プラスFRでショートはボーナス
                        score += funding_rate_bonus_value
                    else:
                        funding_rate_bonus_value = -FUNDING_RATE_BONUS_PENALTY # マイナスFRでショートはペナルティ (過密)
                        score += funding_rate_bonus_value
        
        # G. Dominance Bias Filter (Altcoinにのみ適用)
        # BTCドミナンスのトレンドがAltcoinに有利/不利な場合、ボーナス/ペナルティ
        if symbol != 'BTC-USDT':
            dominance_trend = macro_context.get('dominance_trend', 'Neutral')
            
            # ロングシグナル
            if side == 'ロング':
                if dominance_trend == 'Down': # Altcoinに有利 (ETHがBTCより強い)
                    dominance_bias_bonus_value = DOMINANCE_BIAS_BONUS_PENALTY 
                    score += dominance_bias_bonus_value
                elif dominance_trend == 'Up': # Altcoinに不利
                    dominance_bias_bonus_value = -DOMINANCE_BIAS_BONUS_PENALTY 
                    score += dominance_bias_bonus_value
            
            # ショートシグナル
            elif side == 'ショート':
                if dominance_trend == 'Up': # Altcoinに有利 (BTCが強い=アルトは売られやすい)
                    dominance_bias_bonus_value = DOMINANCE_BIAS_BONUS_PENALTY 
                    score += dominance_bias_bonus_value
                elif dominance_trend == 'Down': # Altcoinに不利
                    dominance_bias_bonus_value = -DOMINANCE_BIAS_BONUS_PENALTY 
                    score += dominance_bias_bonus_value


    # 6. リスクとリワードの計算 (DTS戦略に合わせた計算)
    
    # ATR (Average True Range) が NaN の場合はリスク計算不可
    if np.isnan(atr_val) or atr_val <= 0:
        return {'side': 'DataShortage', 'score': 0.0, 'timeframe': timeframe, 'symbol': symbol, 'macro_context': macro_context}

    
    # Entry Point (Limit Orderを想定したPivot付近のエントリー)
    entry_type = 'Limit'
    entry_price = current_price
    structural_sl_used = False
    
    # 構造的サポート/レジスタンス(R1/S1)の最も近いレベルをエントリーポイントとして採用
    if structural_pivot_bonus > 0:
        
        atr_half = 0.5 * atr_val
        
        if side == 'ロング':
            s1_price = last_row.get('S1', fallback_nan_float)
            s2_price = last_row.get('S2', fallback_nan_float)
            
            # S1/S2の安い方を選ぶ (より深い押し目狙い)
            if not np.isnan(s1_price) and not np.isnan(s2_price):
                entry_price = min(s1_price, s2_price)
            elif not np.isnan(s1_price):
                entry_price = s1_price
            elif not np.isnan(s2_price):
                entry_price = s2_price
            
        elif side == 'ショート':
            r1_price = last_row.get('R1', fallback_nan_float)
            r2_price = last_row.get('R2', fallback_nan_float)

            # R1/R2の高い方を選ぶ (より高い戻り売り狙い)
            if not np.isnan(r1_price) and not np.isnan(r2_price):
                entry_price = max(r1_price, r2_price)
            elif not np.isnan(r1_price):
                entry_price = r1_price
            elif not np.isnan(r2_price):
                entry_price = r2_price
    
    # エントリー価格が現在価格からATRの3倍以上離れている場合は無効
    if abs(entry_price - current_price) > (3.0 * atr_val):
        entry_price = current_price # 成行エントリーに切り替え
        entry_type = 'Market'
    
    
    # Stop Loss Point (初期の追跡ストップ/損切位置)
    # ATRに基づいて計算
    atr_sl_distance = ATR_TRAIL_MULTIPLIER * atr_val
    sl_price = 0.0
    
    if side == 'ロング':
        sl_price_atr = entry_price - atr_sl_distance
        
        # 構造的SL (S1) を使用できるかチェック (ATR-SLより遠く、かつ近いS/Rレベルがある場合)
        s1_price = last_row.get('S1', fallback_nan_float)
        
        if not np.isnan(s1_price) and s1_price < entry_price:
            # S1をSLの候補とする。ただし、S1とエントリーポイントが近すぎる場合はATRをバッファとして追加 (v16.0.1 FIX)
            sl_candidate = s1_price - (0.5 * atr_val) # S1に0.5 ATRのバッファを追加
            
            # ATRによるSLより遠く、かつ現在価格から合理的な範囲内にあるS/Rレベルを使う
            if sl_candidate < sl_price_atr: 
                sl_price = sl_candidate
                structural_sl_used = True
            else:
                sl_price = sl_price_atr # ATRベースを使用
        else:
            sl_price = sl_price_atr # ATRベースを使用

    elif side == 'ショート':
        sl_price_atr = entry_price + atr_sl_distance
        
        # 構造的SL (R1) を使用できるかチェック
        r1_price = last_row.get('R1', fallback_nan_float)

        if not np.isnan(r1_price) and r1_price > entry_price:
            # R1をSLの候補とする。ただし、R1とエントリーポイントが近すぎる場合はATRをバッファとして追加 (v16.0.1 FIX)
            sl_candidate = r1_price + (0.5 * atr_val) # R1に0.5 ATRのバッファを追加
            
            # ATRによるSLより遠く、かつ現在価格から合理的な範囲内にあるS/Rレベルを使う
            if sl_candidate > sl_price_atr:
                sl_price = sl_candidate
                structural_sl_used = True
            else:
                sl_price = sl_price_atr # ATRベースを使用
        else:
            sl_price = sl_price_atr # ATRベースを使用
            
            
    # Take Profit Point (DTS戦略では目標RRR表示用の遠いTPとしてのみ使用)
    # リスク幅 (R) = abs(Entry - SL)
    risk_width = abs(entry_price - sl_price)
    rr_ratio = DTS_RRR_DISPLAY 
    tp_distance = risk_width * rr_ratio
    
    tp_price = 0.0
    if side == 'ロング':
        tp_price = entry_price + tp_distance
    elif side == 'ショート':
        tp_price = entry_price - tp_distance
    
    # リスクリワード比率 (RRR) の計算
    # 損切り幅がゼロになる場合は除外
    risk_width_actual = abs(entry_price - sl_price)
    if risk_width_actual <= 0.000001: 
        rr_ratio_actual = 0.0
    else:
        # DTSでは、実質的なTPは存在しないが、ここでは初期リスクに対する表示上の目標RRRを使用
        rr_ratio_actual = rr_ratio


    # 7. 最終スコアの調整
    score = max(0.01, min(1.0, score)) # スコアを 0.01-1.0 にクリップ

    # 8. 結果の格納
    result = {
        'symbol': symbol,
        'timeframe': timeframe,
        'side': side,
        'score': score,
        'regime': regime,
        'price': current_price,
        'atr_value': atr_val,
        'entry': entry_price,
        'sl': sl_price,
        'tp1': tp_price, # DTSでの遠い目標値
        'rr_ratio': rr_ratio_actual, 
        'entry_type': entry_type,
        'macro_context': macro_context,
        'tech_data': {
            'rsi': rsi_val,
            'macd_hist': macd_hist_val,
            'adx': adx_val,
            'cci': cci_val,
            'bbp': bbp_val,
            'vwap_consistent': (side == 'ロング' and current_price > vwap_val) or (side == 'ショート' and current_price < vwap_val) if not np.isnan(vwap_val) else False,
            'long_term_trend': 'Long' if last_row.get('close', 0) > last_row.get('sma_long', 0) else ('Short' if last_row.get('close', 0) < last_row.get('sma_long', 0) else 'Neutral'),
            'long_term_reversal_penalty': long_term_reversal_penalty,
            'long_term_reversal_penalty_value': LONG_TERM_REVERSAL_PENALTY if long_term_reversal_penalty else 0.0,
            'macd_cross_valid': not macd_cross_penalty,
            'macd_cross_penalty_value': MACD_CROSS_PENALTY if macd_cross_penalty else 0.0,
            'stoch_filter_penalty': stoch_filter_penalty,
            'volume_confirmation_bonus': volume_confirmation_bonus,
            'volume_ratio': volume_ratio,
            'funding_rate_value': 0.0, # fetch_funding_rate(symbol), # 資金調達率は既にマクロコンテキスト内で取得済み
            'funding_rate_bonus_value': funding_rate_bonus_value,
            'structural_pivot_bonus': structural_pivot_bonus,
            'structural_sl_used': structural_sl_used,
            'dominance_trend': macro_context.get('dominance_trend', 'Neutral'),
            'dominance_bias_bonus_value': dominance_bias_bonus_value
        }
    }
    
    return result


async def get_integrated_signals(symbol: str, macro_context: Dict) -> List[Dict]:
    """
    複数の時間軸からデータを取得・分析し、統合されたシグナルリストを生成する
    """
    
    timeframes = ['15m', '1h', '4h']
    all_signals = []
    
    for tf in timeframes:
        # 1. OHLCVデータ取得
        ohlcv, status, client = await fetch_ohlcv_with_fallback(CCXT_CLIENT_NAME, symbol, tf)
        await asyncio.sleep(REQUEST_DELAY_PER_SYMBOL) 
        
        if status != "Success":
            # データ不足や取引所エラーの場合も、結果として記録
            all_signals.append({
                'symbol': symbol,
                'timeframe': tf,
                'side': status, 
                'score': 0.0,
                'macro_context': macro_context,
                'tech_data': {'status': status}
            })
            continue

        # 2. DataFrameの準備
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['close'] = pd.to_numeric(df['close'], errors='coerce')
        df['open'] = pd.to_numeric(df['open'], errors='coerce')
        df['high'] = pd.to_numeric(df['high'], errors='coerce')
        df['low'] = pd.to_numeric(df['low'], errors='coerce')
        df['volume'] = pd.to_numeric(df['volume'], errors='coerce')
        
        # 最新のデータが NaN の場合は無視
        if df['close'].iloc[-1] is np.nan:
            all_signals.append({
                'symbol': symbol,
                'timeframe': tf,
                'side': 'DataShortage', 
                'score': 0.0,
                'macro_context': macro_context,
                'tech_data': {'status': 'Latest close is NaN'}
            })
            continue

        # 3. 単一時間軸の分析
        analysis_result = analyze_single_timeframe(df, tf, symbol, macro_context)
        all_signals.append(analysis_result)
        
    return all_signals

def select_best_signals(all_signals: List[Dict]) -> List[Dict]:
    """
    全てのシグナルから最もスコアの高いシグナルTOP Nを選定する
    """
    
    # 1. 有効かつ閾値以上のシグナルにフィルタリング
    high_score_signals = [
        s for s in all_signals 
        if s.get('side') not in ["DataShortage", "ExchangeError", "Neutral"] 
        and s.get('score', 0.0) >= SIGNAL_THRESHOLD
    ]
    
    if not high_score_signals:
        return []

    # 2. 銘柄ごとの最良シグナルを選定 (最もスコアの高い時間軸を採用)
    best_by_symbol: Dict[str, Dict] = {}
    for signal in high_score_signals:
        symbol = signal['symbol']
        current_best = best_by_symbol.get(symbol)
        
        if current_best is None or signal['score'] > current_best['score']:
            # スコアが同じ場合はR:Rの良さ、ADXの高さ、ATRの低さ（ボラティリティの安定）で順位付け
            if current_best and signal['score'] == current_best['score']:
                 score_criteria = lambda s: (s.get('score', 0.0), s.get('rr_ratio', 0.0), s.get('tech_data', {}).get('adx', 0.0), -s.get('tech_data', {}).get('atr_value', 1.0))
                 if score_criteria(signal) > score_criteria(current_best):
                     best_by_symbol[symbol] = signal
                 
            else:
                best_by_symbol[symbol] = signal
            
    # 3. 最終的なランキング
    final_ranked_signals = sorted(
        best_by_symbol.values(),
        key=lambda s: (
            s.get('score', 0.0), 
            s.get('rr_ratio', 0.0), 
            s.get('tech_data', {}).get('adx', 0.0), 
            -s.get('tech_data', {}).get('atr_value', 1.0)
        ),
        reverse=True
    )

    # 4. TOP N (TOP_SIGNAL_COUNT) に絞り込み
    return final_ranked_signals[:TOP_SIGNAL_COUNT]


# ====================================================================================
# MAIN PROCESS
# ====================================================================================

async def analyze_all_symbols():
    """全監視銘柄の分析と、優秀なシグナルの通知を行う"""
    global CURRENT_MONITOR_SYMBOLS, LAST_ANALYSIS_SIGNALS, LAST_SUCCESS_TIME, GLOBAL_MACRO_CONTEXT
    
    logging.info(f"💡 分析開始。監視銘柄数: {len(CURRENT_MONITOR_SYMBOLS)}")
    
    # 1. マクロコンテキストの取得 (最初に1回だけ実行)
    GLOBAL_MACRO_CONTEXT = await get_crypto_macro_context()
    
    all_signals_raw: List[Dict] = []
    
    # 2. 全銘柄の分析を並行して実行
    analysis_tasks = []
    for symbol in CURRENT_MONITOR_SYMBOLS:
        analysis_tasks.append(get_integrated_signals(symbol, GLOBAL_MACRO_CONTEXT))
        
    results = await asyncio.gather(*analysis_tasks)
    
    # 3. 結果の統合
    for symbol_signals in results:
        for signal in symbol_signals:
            all_signals_raw.append(signal)

    # 4. 最適シグナルの選定とランキング付け
    best_signals = select_best_signals(all_signals_raw)
    
    # 5. ログと通知
    LAST_ANALYSIS_SIGNALS = best_signals 
    LAST_SUCCESS_TIME = time.time()
    
    logging.info(f"✅ 全銘柄の分析が完了しました。有効なトップシグナル数: {len(best_signals)}")
    
    if not best_signals:
        logging.info("閾値 (0.75) を超える有効なシグナルは見つかりませんでした。")
        return 
        
    notified_count = 0
    current_time = time.time()
    
    # 通知は最もスコアが高いシグナルのみに限定する
    for rank, best_signal in enumerate(best_signals, 1):
        
        symbol = best_signal['symbol']
        
        # クールダウンチェック (前回通知から一定時間経過しているか)
        last_notified = TRADE_NOTIFIED_SYMBOLS.get(symbol, 0.0)
        if current_time - last_notified < TRADE_SIGNAL_COOLDOWN:
            logging.info(f"⏸️ {symbol} はクールダウン期間中です (前回通知: {datetime.fromtimestamp(last_notified, tz=JST).strftime('%Y-%m-%d %H:%M')})")
            continue
            
        # 該当銘柄の全時間軸の分析結果を抽出
        all_signals_for_symbol = [s for s in all_signals_raw if s['symbol'] == symbol]

        # Telegramメッセージの整形
        message = format_integrated_analysis_message(symbol, all_signals_for_symbol, rank)
        
        if message:
            send_telegram_html(message)
            TRADE_NOTIFIED_SYMBOLS[symbol] = current_time
            notified_count += 1
            # ログ出力
            score_100 = best_signal['score'] * 100
            logging.warning(f"🔔 TOP{rank} シグナル通知 ({symbol} - {best_signal['timeframe']} {best_signal['side']}) スコア: {score_100:.2f}点")
        
        if notified_count >= TOP_SIGNAL_COUNT:
            break
            
    # 古いクールダウンエントリを削除 (念のため)
    cutoff_time = current_time - TRADE_SIGNAL_COOLDOWN * 2
    keys_to_delete = [s for s, t in TRADE_NOTIFIED_SYMBOLS.items() if t < cutoff_time]
    for key in keys_to_delete:
        del TRADE_NOTIFIED_SYMBOLS[key]


async def main_loop():
    """BOTのメインループ"""
    global LAST_UPDATE_TIME, EXCHANGE_CLIENT
    
    await initialize_ccxt_client()

    while True:
        try:
            # --- ヒートマップ機能の追加 (1時間ごと) ---
            await create_btc_heatmap_and_notify()
            # ----------------------------------------
            
            # 1. 銘柄リストの更新 (定期的に実行)
            await update_symbols_by_volume()
            
            # 2. 分析とシグナル通知の実行
            await analyze_all_symbols()
            
            # 3. 待機
            LAST_UPDATE_TIME = time.time()
            await asyncio.sleep(LOOP_INTERVAL)

        except Exception as e:
            error_name = type(e).__name__
            if EXCHANGE_CLIENT:
                 await EXCHANGE_CLIENT.close()
                 EXCHANGE_CLIENT = None
                 await initialize_ccxt_client()
            else:
                 pass 
            
            logging.error(f"メインループで致命的なエラー: {error_name}")
            await asyncio.sleep(60)


# ====================================================================================
# FASTAPI SETUP
# ====================================================================================

app = FastAPI(title="Apex BOT API", version="v16.0.2 - Heatmap Feature Add") # バージョン更新

@app.on_event("startup")
async def startup_event():
    logging.info("🚀 Apex BOT v16.0.2 Startup initializing...") # バージョン更新
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
        "bot_version": "v16.0.2 - Heatmap Feature Add", # バージョン更新
        "last_success_time_utc": datetime.fromtimestamp(LAST_SUCCESS_TIME, tz=timezone.utc).isoformat() if LAST_SUCCESS_TIME else "N/A",
        "current_client": CCXT_CLIENT_NAME,
        "monitoring_symbols": len(CURRENT_MONITOR_SYMBOLS),
        "last_signals_count": len(LAST_ANALYSIS_SIGNALS)
    }
    return JSONResponse(content=status_msg)

@app.head("/")
@app.get("/")
def home_view():
    return JSONResponse(content={"message": "Apex BOT is running (v16.0.2 - Heatmap Feature Add)"})

if __name__ == "__main__":
    # Windowsで動かす場合は以下をコメントアウトし、uvicorn.run() を使う 
    # if sys.platform == 'win32':
    #     asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy()) 
        
    uvicorn.run(app, host="0.0.0.0", port=8000)
