"""Product navigation from the observed Tongcheng product_simple_card format.

Card prices and marketing HTML are deliberately excluded from model context.
"""
import json
import re
from urllib.parse import urlsplit


def product_card(content):
    try:
        card=json.loads(content) if isinstance(content,str) else content
        if not isinstance(card,dict) or card.get('bizType')!='product_simple_card':return None
        rows=card.get('data')
        if not isinstance(rows,list) or not 1<=len(rows)<=20:return []
        products=[]
        for row in rows:
            if not isinstance(row,dict):return []
            identifier=str(row.get('serialId',''));title=row.get('title')
            link=urlsplit(row.get('detailLink',''))
            match=re.fullmatch(r'/journey/tours/([1-9]\d{0,11})',link.path)
            if not match or link.scheme!='https' or link.hostname not in ('wx.17u.cn','m.ly.com') or match[1]!=identifier:return []
            if link.hostname=='m.ly.com':
                mirror=urlsplit(row.get('wechatProgramUrl',''))
                if mirror.scheme!='https' or mirror.hostname!='wx.17u.cn' or mirror.path!=link.path:return []
            if not isinstance(title,str) or not title.strip() or len(title)>500:return []
            item={'platform':'tongcheng','external_product_id':identifier,'title':title.strip(),
                'source':'platform_product_card','reference_source':'platform_card_serial_id_and_url','reviewed':False}
            for source,target in [('setMeal','package_label'),('departureCity','departure_city')]:
                if isinstance(row.get(source),str):item[target]=row[source][:100]
            if item not in products:products.append(item)
        return products
    except (ValueError,TypeError):return []
