import ccxt.pro as ccxt
import asyncio
from typing import Dict, Any, List, Optional
import logging
import os
import random
import time
from datetime import datetime
import telegram

# ====================================================================================
# ロギング設定
# ====================================================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)

# ====================================================================================
# 設定変数
# ====================================================================================
# API設定
API_KEYS: Dict[str, Dict[str, str]] = {
    'okx': {
        'apiKey': os.environ.get('OKX_API_KEY', 'YOUR_OKX_API_KEY'),
        'secret': os.environ.get('OKX_SECRET_KEY', 'YOUR_OKX_SECRET_KEY'),
        'password': os.environ.get('OKX_PASSWORD', 'YOUR_OKX_PASSWORD'),
        'options': {'defaultType': 'swap'}, # OKXは先物(SWAP)に設定
    },
    'coinbase': {
        'apiKey': os.environ.get('COINBASE_API_KEY', 'YOUR_COINBASE_API_KEY'),
        'secret': os.environ.get('COINBASE_SECRET_KEY', 'YOUR_COINBASE_SECRET_KEY'),
        'options': {'defaultType': 'spot'}, # Coinbaseは現物(SPOT)に設定
    },
    'kraken': {
        'apiKey': os.environ.get('KRAKEN_API_KEY', 'YOUR_KRAKEN_API_KEY'),
        'secret': os.environ.get('KRAKEN_SECRET_KEY', 'YOUR_KRAKEN_SECRET_KEY'),
        'options': {'defaultType': 'spot'}, # Krakenは現物(SPOT)に設定
    },
}

# CCXTクライアント
CCXT_CLIENTS_DICT: Dict[str, ccxt.Exchange] = {}
CCXT_CLIENT_NAMES: List[str] = ['okx', 'coinbase', 'kraken']

# マッピング設定 (KrakenのBTC/USDTをXBT/USDTに変換するため)
SYMBOL_MAPPING: Dict[str, Dict[str, str]] = {
    'kraken': {
        "BTC/USDT": "XBT/USDT", # KrakenのBTCシンボルはXBT
    },
}

# ボット設定
TIME_FRAME: str = '5m'
DYNAMIC_UPDATE_INTERVAL: int = 60 * 30 # 30分ごとに動的に銘柄リストを更新
CLIENT_SWITCH_INTERVAL: int = 60 * 60 # 60分ごとにクライアントを切り替え
TOP_VOLUME_LIMIT: int = 30 # 出来高トップN銘柄を選択
QUOTE_CURRENCY: str = 'USDT' # 出来高の基準とする通貨
INITIAL_FALLBACK_SYMBOLS: List[str] = ['BTC/USDT', 'ETH/USDT'] # 初回またはエラー時のフォールバック銘柄

# グローバル状態変数
CCXT_CLIENT_NAME: str = ''
CURRENT_MONITOR_SYMBOLS: List[str] = []
LAST_UPDATE_TIME: float = 0
LAST_SWITCH_TIME: float = 0
CCXT_CLIENT_HEALTH: Dict[str, Dict[str, Any]] = {} # クライアントの稼働状態を追跡

# Telegram設定 (Render環境変数から取得)
TELEGRAM_BOT_TOKEN: Optional[str] = os.environ.get('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID: Optional[str] = os.environ.get('TELEGRAM_CHAT_ID')
TELEGRAM_BOT: Optional[telegram.Bot] = None

# ====================================================================================
# ヘルパー関数
# ====================================================================================

def get_mapped_symbol(client_name: str, symbol: str) -> str:
    """クライアント固有のシンボルマッピングを適用"""
    return SYMBOL_MAPPING.get(client_name, {}).get(symbol, symbol)

def get_mapped_timeframe(client_name: str, timeframe: str) -> str:
    """クライアント固有のタイムフレームマッピングを適用（今回は省略）"""
    return timeframe

async def send_telegram_message(message: str, client_name: str = 'System'):
    """Telegram通知を送信"""
    if TELEGRAM_BOT:
        try:
            await TELEGRAM_BOT.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"[{client_name}] {message}")
        except Exception as e:
            logging.error(f"❌ Telegram通知の送信に失敗しました: {e}")
            
# ====================================================================================
# 初期化関数
# ====================================================================================

def initialize_telegram():
    """Telegramクライアントを初期化"""
    global TELEGRAM_BOT
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        TELEGRAM_BOT = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
        logging.info("✅ Telegramクライアント初期化完了。")
    else:
        logging.warning("⚠️ TelegramトークンまたはChat IDが設定されていません。通知は無効です。")

def initialize_ccxt_client():
    """CCXTクライアントを初期化し、グローバル辞書に格納"""
    global CCXT_CLIENTS_DICT, CCXT_CLIENT_HEALTH
    available_clients = []
    
    for name in CCXT_CLIENT_NAMES:
        params = API_KEYS.get(name, {})
        if params.get('apiKey') in ('YOUR_OKX_API_KEY', None) and name != 'kraken':
             # APIキーがない場合はパブリックアクセスのみでインスタンス化（非推奨だが続行）
             logging.warning(f"⚠️ {name} のAPIキーが設定されていません。パブリックアクセスのみで初期化します。")
        
        try:
            # ccxt.pro クライアントを動的に生成
            exchange_class = getattr(ccxt, name)
            client = exchange_class(params)
            
            CCXT_CLIENTS_DICT[name] = client
            CCXT_CLIENT_HEALTH[name] = {'status': 'ok', 'cooldown_until': 0}
            available_clients.append(name.upper())
            
        except Exception as e:
            logging.error(f"❌ CCXTクライアント {name} の初期化に失敗しました: {e}")

    if not CCXT_CLIENTS_DICT:
        logging.critical("❌ 全てのCCXTクライアントの初期化に失敗しました。プログラムを終了します。")
        raise SystemExit("CCXTクライアントの初期化エラー")
        
    logging.info(f"✅ CCXTクライアント初期化完了。利用可能なクライアント: {available_clients}")
    
# ====================================================================================
# 動的銘柄選択ロジック (出来高トップ30)
# ====================================================================================

async def fetch_top_volume_symbols(client: ccxt.Exchange) -> List[str]:
    """
    CCXTクライアントから出来高トップのUSDTペア銘柄を取得する
    """
    client_name = client.id
    if not client.has['fetchTickers']:
        logging.warning(f"⚠️ クライアント {client_name} は fetch_tickers をサポートしていません。出来高トップ銘柄の取得をスキップします。")
        return []
    
    try:
        # fetch_tickersで全銘柄のティッカー情報を取得
        tickers = await client.fetch_tickers()
        
        # 1. USDTペアかつ出来高情報(quoteVolume)がある銘柄のみにフィルタリング
        usdt_pairs = {
            symbol: ticker for symbol, ticker in tickers.items() 
            if symbol.endswith(f'/{QUOTE_CURRENCY}') and 
               ticker and 
               ticker.get('quoteVolume') is not None and 
               float(ticker.get('quoteVolume', 0)) > 0 # 出来高が0でないことを確認
        }
        
        if not usdt_pairs:
            logging.warning(f"⚠️ {client_name} では {QUOTE_CURRENCY} ペアの出来高情報が見つかりませんでした。")
            return []

        # 2. quoteVolumeで降順にソート
        # quoteVolumeの値はfloatに変換して比較
        sorted_pairs = sorted(
            usdt_pairs.items(), 
            key=lambda item: float(item[1]['quoteVolume']), 
            reverse=True
        )

        # 3. トップ N 銘柄を選択
        top_n_symbols = [symbol for symbol, _ in sorted_pairs[:TOP_VOLUME_LIMIT]]
        
        logging.info(f"✅ {client_name} から出来高トップ {TOP_VOLUME_LIMIT} の銘柄を取得しました。総対象ペア数: {len(usdt_pairs)}")
        return top_n_symbols

    except Exception as e:
        logging.error(f"❌ {client_name} で出来高トップ銘柄の取得に失敗しました: {e}. CCXT Health Reset...")
        # 失敗した場合はクライアントのヘルスをリセット
        CCXT_CLIENT_HEALTH[client_name]['status'] = 'cooldown'
        CCXT_CLIENT_HEALTH[client_name]['cooldown_until'] = time.time() + 300 # 5分クールダウン
        await send_telegram_message(f"出来高取得エラー発生により {client_name} を5分間クールダウンします。", client_name)
        return []

async def update_monitor_symbols_dynamically():
    """
    監視銘柄リストを更新（現在のクライアントの出来高トップ30に更新）
    """
    global CURRENT_MONITOR_SYMBOLS
    logging.info(f"🔄 銘柄リストを出来高トップ {TOP_VOLUME_LIMIT} に更新します (クライアント: {CCXT_CLIENT_NAME})。")
    
    current_client = CCXT_CLIENTS_DICT[CCXT_CLIENT_NAME]
    
    # 出来高トップ銘柄を取得する
    filtered_symbols = await fetch_top_volume_symbols(current_client) 

    if not filtered_symbols:
        logging.warning(f"⚠️ クライアント {CCXT_CLIENT_NAME} で監視対象銘柄が見つかりませんでした。フォールバック銘柄を使用します。")
        CURRENT_MONITOR_SYMBOLS = INITIAL_FALLBACK_SYMBOLS
    else:
        CURRENT_MONITOR_SYMBOLS = filtered_symbols
        
    logging.info(f"✅ クライアント {CCXT_CLIENT_NAME} の分析対象銘柄リスト: ({len(CURRENT_MONITOR_SYMBOLS)}銘柄)")
    await asyncio.sleep(0.5)

# ====================================================================================
# データ取得と分析ロジック
# ====================================================================================

async def fetch_ohlcv_with_fallback(client: ccxt.Exchange, symbol: str, timeframe: str) -> Optional[List[List[float]]]:
    """
    OHLCVデータを取得し、NotSupportedエラーを捕捉してクライアントをクールダウン
    """
    client_name = client.id
    mapped_symbol = get_mapped_symbol(client_name, symbol)
    mapped_timeframe = get_mapped_timeframe(client_name, timeframe)
    
    try:
        # OHLCVデータを取得
        ohlcv = await client.fetch_ohlcv(mapped_symbol, mapped_timeframe, limit=200)
        return ohlcv
        
    except ccxt.NotSupported as e:
        # CCXT側でサポートされていないシンボルやタイムフレームの場合
        logging.error(f"❌ NotSupportedエラー発生: クライアント {client_name} はシンボル {symbol} ({mapped_symbol}) をサポートしていません。")
        # 特定のシンボルのエラーなので、クライアント全体はクールダウンしない
        return None
        
    except (ccxt.NetworkError, ccxt.ExchangeError) as e:
        # その他のネットワーク/取引所エラーが発生した場合、クライアントをクールダウン
        cool_down_until = time.time() + 300
        CCXT_CLIENT_HEALTH[client_name]['status'] = 'cooldown'
        CCXT_CLIENT_HEALTH[client_name]['cooldown_until'] = cool_down_until
        
        logging.error(f"❌ CCXTエラー発生: クライアント {client_name} のヘルスを {datetime.fromtimestamp(cool_down_until).strftime('%H:%M:%S JST')} にリセット (クールダウン: 300s)。")
        await send_telegram_message(f"CCXTエラー: {e}。{client_name} を5分間クールダウンします。", client_name)
        return None
        
    except Exception as e:
        logging.error(f"❌ 予期せぬエラーが発生しました ({client_name}, {symbol}): {e}")
        return None

async def generate_signal_candidate(client: ccxt.Exchange, symbol: str, timeframe: str):
    """
    単一銘柄のOHLCVを取得し、簡単な分析を実行（今回はデータ取得のみ）
    """
    ohlcv = await fetch_ohlcv_with_fallback(client, symbol, timeframe)
    
    if ohlcv is None or len(ohlcv) < 5:
        return # データ不足またはエラーのためスキップ

    # 最後のローソク足の情報を取得
    last_candle = ohlcv[-1]
    close_price = last_candle[4]
    volume = last_candle[5]

    # 仮の分析: 出来高が過去5本の平均より高い場合を「注目」とする
    past_volumes = [c[5] for c in ohlcv[-6:-1]] # 最新を除く過去5本
    avg_volume = sum(past_volumes) / 5 if past_volumes else 0
    
    if volume > avg_volume * 1.5 and avg_volume > 0:
        # 強力なシグナル候補としてログ出力
        logging.warning(f"🔥 強力な出来高シグナル: {symbol} @ {client.id.upper()} | 終値: {close_price:.4f} | 出来高: {volume:.2f} (平均の x{volume/avg_volume:.2f})")
        # Telegram通知はノイズになるため、ここでは省略
        
    else:
        # 通常の分析ログ
        logging.debug(f"🔍 分析完了: {symbol} @ {client.id.upper()} | 終値: {close_price:.4f} | 出来高: {volume:.2f}")


# ====================================================================================
# メインループ
# ====================================================================================

async def main_loop():
    """ボットのメイン実行ループ"""
    global CCXT_CLIENT_NAME, LAST_UPDATE_TIME, LAST_SWITCH_TIME

    # 起動時のクライアント選択 (ランダム)
    CCXT_CLIENT_NAME = random.choice(list(CCXT_CLIENTS_DICT.keys()))
    LAST_SWITCH_TIME = time.time()
    
    # 初回起動通知
    await send_telegram_message(f"🚀 Apex BOT v9.1.18-DynamicVolumeSelector Startup Complete. Initial Client: {CCXT_CLIENT_NAME.upper()}", 'System')

    while True:
        current_time = time.time()
        
        # --- 1. クライアントの選択と切り替え ---
        available_clients = [name for name, health in CCXT_CLIENT_HEALTH.items() if health['status'] == 'ok']
        
        if current_time - LAST_SWITCH_TIME > CLIENT_SWITCH_INTERVAL and available_clients:
            # クライアントを切り替え
            old_client_name = CCXT_CLIENT_NAME
            new_client_name = random.choice(available_clients)
            
            if new_client_name != old_client_name:
                CCXT_CLIENT_NAME = new_client_name
                LAST_SWITCH_TIME = current_time
                logging.info(f"🔄 クライアントを {old_client_name.upper()} から {CCXT_CLIENT_NAME.upper()} に切り替えました。")
                await send_telegram_message(f"🔄 クライアントを {old_client_name.upper()} から {CCXT_CLIENT_NAME.upper()} に切り替えました。", 'System')
            else:
                 # ランダム選択の結果、同じクライアントになった場合も時間を更新して続行
                LAST_SWITCH_TIME = current_time

        elif not available_clients:
            logging.critical("❌ 全てのクライアントがクールダウン中です。5分間待機します。")
            await asyncio.sleep(300)
            continue
            
        # クールダウン中のクライアントをスキップ
        if CCXT_CLIENT_HEALTH[CCXT_CLIENT_NAME]['status'] != 'ok':
            cool_down_until = CCXT_CLIENT_HEALTH[CCXT_CLIENT_NAME]['cooldown_until']
            remaining = cool_down_until - current_time
            if remaining > 0:
                logging.warning(f"⏳ クライアント {CCXT_CLIENT_NAME.upper()} はクールダウン中です。残り {remaining:.0f} 秒。次のクライアントを選択します。")
                
                # クールダウン解除されたクライアントがないかチェック
                for name, health in CCXT_CLIENT_HEALTH.items():
                    if health['status'] == 'cooldown' and current_time >= health['cooldown_until']:
                        health['status'] = 'ok'
                        logging.info(f"✅ クライアント {name.upper()} のクールダウンが解除されました。")
                        await send_telegram_message(f"✅ クライアント {name.upper()} のクールダウンが解除されました。", name)
                
                # 別の利用可能なクライアントを選択して再試行
                new_available = [name for name, health in CCXT_CLIENT_HEALTH.items() if health['status'] == 'ok']
                if new_available:
                    CCXT_CLIENT_NAME = random.choice(new_available)
                    continue # ループの最初に戻り、新しいクライアントで処理
                else:
                    logging.critical("❌ 全てのクライアントがクールダウン中のため待機します。")
                    await asyncio.sleep(30)
                    continue
            else:
                # クールダウン時間が経過
                CCXT_CLIENT_HEALTH[CCXT_CLIENT_NAME]['status'] = 'ok'
                logging.info(f"✅ クライアント {CCXT_CLIENT_NAME.upper()} のクールダウンが解除されました。")
                await send_telegram_message(f"✅ クライアント {CCXT_CLIENT_NAME.upper()} のクールダウンが解除されました。", CCXT_CLIENT_NAME)


        # --- 2. 動的銘柄リストの更新 (出来高トップ30を取得) ---
        if current_time - LAST_UPDATE_TIME > DYNAMIC_UPDATE_INTERVAL or not CURRENT_MONITOR_SYMBOLS:
            await update_monitor_symbols_dynamically() 
            LAST_UPDATE_TIME = current_time

        # --- 3. 分析の実行 ---
        if CURRENT_MONITOR_SYMBOLS:
            client = CCXT_CLIENTS_DICT[CCXT_CLIENT_NAME]
            logging.info(f"🔍 分析開始 (データソース: {CCXT_CLIENT_NAME.upper()}, 銘柄数: {len(CURRENT_MONITOR_SYMBOLS)}銘柄)")
            
            # 各銘柄に対して並行してOHLCV取得と分析を実行
            analysis_tasks = [
                generate_signal_candidate(client, symbol, TIME_FRAME)
                for symbol in CURRENT_MONITOR_SYMBOLS
            ]
            
            # 最大20秒まで待機（APIレートリミットを考慮して調整）
            try:
                await asyncio.wait_for(asyncio.gather(*analysis_tasks), timeout=20.0)
            except asyncio.TimeoutError:
                logging.warning(f"⏳ 分析タスクがタイムアウトしました ({CCXT_CLIENT_NAME.upper()})。レートリミットに注意してください。")
            except Exception as e:
                logging.error(f"❌ 分析タスク中に予期せぬエラーが発生しました: {e}")
                
        else:
            logging.warning("⚠️ 監視対象銘柄リストが空です。次の更新まで待機します。")

        # レートリミットを尊重し、次のループまで待機
        await asyncio.sleep(5) 

# ====================================================================================
# アプリケーション起動
# ====================================================================================

# Uvicorn/Renderデプロイ用に `main_render.py` から参照されるエントリポイント
def app_startup():
    """アプリケーションの起動ロジック"""
    
    # 1. 初期化
    initialize_telegram()
    initialize_ccxt_client()
    
    # 2. メインループの実行
    try:
        # 非同期メインループを開始
        asyncio.run(main_loop())
        
    except KeyboardInterrupt:
        logging.info("ボットを停止します。")
        pass
    except SystemExit:
        logging.critical("初期化エラーにより停止しました。")
    except Exception as e:
        logging.critical(f"ボット実行中に致命的なエラーが発生しました: {e}", exc_info=True)
    finally:
        # クライアント接続を閉じる
        for client in CCXT_CLIENTS_DICT.values():
            asyncio.run(client.close())
        logging.info("全てのCCXT接続を閉じました。")

if __name__ == "__main__":
    # Renderデプロイ環境では uvicorn でメインループを起動するため、このブロックは実行されないことが多い
    # ローカル実行テスト用
    app_startup()

# ====================================================================================
# Render/Uvicorn用エントリーポイント (main_render.py に記述する場合の雛形)
# ====================================================================================
# Uvicorn/FastAPIのアプリケーション構造に合わせるため、以下を別ファイル (main_render.py) に記述することが一般的。
# Renderがログに表示する 'uvicorn main_render:app' の 'app' は、
# 起動時に一度実行される非同期関数またはASGIアプリケーションを指します。

# import uvicorn
# from fastapi import FastAPI
# from main_apex_mtfa_v_dynamic_volume_selector import app_startup 

# app = FastAPI()

# @app.on_event("startup")
# async def startup_event():
#     # バックグラウンドでボットのメインループを実行
#     asyncio.create_task(app_startup())
    
# # Uvicornの起動コマンドは: uvicorn main_render:app --host 0.0.0.0 --port $PORT
# # このメインファイルでは、app_startup()を直接呼び出すことで実行可能。
