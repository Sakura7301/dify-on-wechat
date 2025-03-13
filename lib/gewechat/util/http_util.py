import requests  
import json  
import os  
from config import conf

# 使用缓存机制存储代理配置  
_proxy_config = None  

def load_proxy_config():  
    global _proxy_config  
    if _proxy_config is None:  # 如果代理配置未加载，则加载一次  
        _proxy_config = conf().get("http_proxies") 
    return _proxy_config  

def post_json(base_url, route, token, data):  
    headers = {  
        'Content-Type': 'application/json'  
    }  
    if token:  
        headers['X-GEWE-TOKEN'] = token  

    url = base_url + route  

    proxies = load_proxy_config()  # 从缓存中获取代理配置  

    try:  
        if proxies:  
            response = requests.post(url, json=data, headers=headers, timeout=60, proxies=proxies)  
        else:  
            response = requests.post(url, json=data, headers=headers, timeout=60)  
        response.raise_for_status()  
        result = response.json()  

        if result.get('ret') == 200:  
            return result  
        else:  
            raise RuntimeError(response.text)  
    except Exception as e:  
        print(f"http请求失败, url={url}, exception={e}")  
        raise RuntimeError(str(e))  