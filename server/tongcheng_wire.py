"""Minimal bounded codec for the publicly shipped holiday Protobuf schema.

Field numbers are from the platform web SDK; no generated/vendor SDK is bundled.
"""
import json

def varint(value):
    if not isinstance(value,int) or not 0<=value<2**64:raise ValueError('invalid_integer')
    out=bytearray()
    while value>127:out.append((value&127)|128);value>>=7
    out.append(value);return bytes(out)

def integer(field,value):return varint(field<<3)+varint(value)
def blob(field,value):
    value=value.encode() if isinstance(value,str) else value
    return varint((field<<3)|2)+varint(len(value))+value

def fields(data):
    if not isinstance(data,bytes) or len(data)>1024*1024:raise ValueError('invalid_packet')
    pos=0;out={}
    def read():
        nonlocal pos
        value=0
        for shift in range(0,70,7):
            if pos>=len(data):raise ValueError('truncated_varint')
            b=data[pos];pos+=1;value|=(b&127)<<shift
            if b<128:return value
        raise ValueError('oversized_varint')
    while pos<len(data):
        key=read();field,wire=key>>3,key&7
        if not field:raise ValueError('invalid_field')
        if wire==0:value=read()
        elif wire==2:
            size=read()
            if size>len(data)-pos:raise ValueError('truncated_field')
            value=data[pos:pos+size];pos+=size
        elif wire in (1,5):
            size=8 if wire==1 else 4
            if size>len(data)-pos:raise ValueError('truncated_field')
            value=data[pos:pos+size];pos+=size
        else:raise ValueError('unsupported_wire_type')
        out[field]=value
    return out

def packet(sid,cmd,body=b''):return integer(1,sid)+integer(2,cmd)+(blob(3,body) if body else b'')
def login_body(session):
    return integer(1,int(session['userId']))+blob(2,session['accessToken'])+blob(3,session['spaceId'])+blob(6,session['deviceId'])+blob(11,session['appId'])
def text_packet(session,customer,text,fingerprint):
    body={'cy':0,'f':session['source_uid'],'t':customer,'m':text,'ty':0}
    protocol={'type':'2','dataContent':json.dumps(body,ensure_ascii=False,separators=(',',':')),
        'from':str(session['source_uid']),'to':str(customer),'typeu':3,'fp':fingerprint,'QoS':True,'tc':session['tenant_code']}
    return blob(1,json.dumps(protocol,ensure_ascii=False,separators=(',',':')))
