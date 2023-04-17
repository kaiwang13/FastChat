import requests


proxies = {
   'http': 'http://wkpc:1080',
   'https': 'http://wkpc:1080',
}

def detect_language(content):
    url = "https://translation.googleapis.com/language/translate/v2/detect"
    data = {
        'key':"AIzaSyDFkW4HYTd5cnt_Lh8GJrfrHm5tRhZdiyY",
        'q': content,
        'format': "text"
    }
    response = requests.post(url, data, proxies=proxies)
    res = response.json()
    result = res["data"]["detections"][0][0]["language"]
    return result

def translate_to_en(content, language):
    url = "https://translation.googleapis.com/language/translate/v2"
    data = {
        'key':"AIzaSyDFkW4HYTd5cnt_Lh8GJrfrHm5tRhZdiyY",
        'source': language,
        'target': 'en-us',
        'q': content,
        'format': "text"
    }
    response = requests.post(url, data, proxies=proxies)
    res = response.json()
    result = res["data"]["translations"][0]["translatedText"]
    return result

def translate_from_en(content, language):
    url = "https://translation.googleapis.com/language/translate/v2"
    data = {
        'key':"AIzaSyDFkW4HYTd5cnt_Lh8GJrfrHm5tRhZdiyY",
        'source': 'en-us',
        'target': language,
        'q': content,
        'format': "text"
    }
    response = requests.post(url, data, proxies=proxies)
    res = response.json()
    result = res["data"]["translations"][0]["translatedText"]
    return result
