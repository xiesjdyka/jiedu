"""Verify the approved v1 door artwork and derive raw window pixels unchanged.

No image generation, resizing, palette changes or artwork replacement occurs.
The ICO, tray and web assets remain the reviewed originals.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import struct
import zlib

ICO_SHA256 = 'bd2a59f784fcc8be199d0f7278537c70d2cf09cdcd232dc39a684499591557c7'
TRAY_SHA256 = '2b35a5c8a7540053f59a526f832fffe9993ab4506114b942dfe71d04f94f8a85'

def ico_frames(data):
    reserved, kind, count = struct.unpack_from('<HHH', data)
    if reserved or kind != 1 or not 1 <= count <= 32:
        raise ValueError('Invalid ICO header')
    frames = {}
    for index in range(count):
        w,h,_,zero,planes,bits,length,offset = struct.unpack_from('<BBBBHHII', data, 6 + 16*index)
        size=(w or 256,h or 256)
        if zero or planes not in (0,1) or bits not in (0,32) or offset < 6+16*count or offset+length > len(data) or size in frames:
            raise ValueError('Invalid ICO frame')
        frames[size]=data[offset:offset+length]
    return frames

def _paeth(a,b,c):
    p=a+b-c;d=(abs(p-a),abs(p-b),abs(p-c))
    return (a,b,c)[d.index(min(d))]

def png_rgba(data):
    if not data.startswith(b'\x89PNG\r\n\x1a\n'):
        raise ValueError('Not PNG')
    offset=8;payload=[];header=None;ended=False
    while offset+12 <= len(data):
        length=struct.unpack_from('>I',data,offset)[0];kind=data[offset+4:offset+8]
        value=data[offset+8:offset+8+length]
        if offset+12+length > len(data) or zlib.crc32(kind+value)&0xffffffff != struct.unpack_from('>I',data,offset+8+length)[0]:
            raise ValueError('Invalid PNG chunk')
        offset+=12+length
        if kind==b'IHDR':header=struct.unpack('>IIBBBBB',value)
        elif kind==b'IDAT':payload.append(value)
        elif kind==b'IEND':ended=True;break
    if not ended or offset!=len(data) or header is None:
        raise ValueError('Incomplete PNG')
    width,height,depth,colour,compression,filtering,interlace=header
    if not 1<=width<=256 or not 1<=height<=256 or depth!=8 or colour not in (2,6) or compression or filtering or interlace:
        raise ValueError('Unsupported approved icon PNG layout')
    bpp=4 if colour==6 else 3;stride=width*bpp
    decoder=zlib.decompressobj();raw=decoder.decompress(b''.join(payload),(stride+1)*height+1)
    if len(raw)!=(stride+1)*height or not decoder.eof or decoder.unused_data:
        raise ValueError('Invalid PNG pixels')
    rgba=bytearray();previous=bytearray(stride)
    for y in range(height):
        row=bytearray(raw[y*(stride+1)+1:(y+1)*(stride+1)]);method=raw[y*(stride+1)]
        if method>4:raise ValueError('Invalid PNG filter')
        for x in range(stride):
            a=row[x-bpp] if x>=bpp else 0;b=previous[x];c=previous[x-bpp] if x>=bpp else 0
            row[x]=(row[x]+(0,a,b,(a+b)//2,_paeth(a,b,c))[method])&255
        if bpp==4:rgba.extend(row)
        else:
            for x in range(0,stride,3):rgba.extend(row[x:x+3]+b'\xff')
        previous=row
    return width,height,bytes(rgba)

def frame_rgba(data, size):
    if data.startswith(b'\x89PNG'):
        width,height,pixels=png_rgba(data)
        if (width,height)!=size:raise ValueError('PNG/ICO dimensions differ')
        return pixels
    header,width,height,planes,bits,compression=struct.unpack_from('<IiiHHI',data)
    if header<40 or (width,height)!=(size[0],size[1]*2) or planes!=1 or bits!=32 or compression:
        raise ValueError('Unsupported approved icon bitmap')
    stride=width*4
    if len(data)<header+stride*size[1]:raise ValueError('Truncated icon bitmap')
    result=bytearray()
    for y in range(size[1]-1,-1,-1):
        row=data[header+y*stride:header+(y+1)*stride]
        for x in range(0,stride,4):result.extend((row[x+2],row[x+1],row[x],row[x+3]))
    if not any(result[3::4]):raise ValueError('Approved icon must carry explicit alpha')
    return bytes(result)

def verify_assets(source, *, write_window=False):
    source=Path(source);icons=source/'desktop/icons'
    ico=(icons/'icon.ico').read_bytes();tray=(icons/'tray.rgba').read_bytes()
    if hashlib.sha256(ico).hexdigest()!=ICO_SHA256 or hashlib.sha256(tray).hexdigest()!=TRAY_SHA256 or len(tray)!=1024:
        raise ValueError('Approved v1 door artwork changed')
    frames=ico_frames(ico)
    if set(frames)!={(n,n) for n in (16,20,24,32,40,48,64,96,128,256)}:
        raise ValueError('Approved multi-size ICO is incomplete')
    pixels=frame_rgba(frames[(32,32)],(32,32))
    window=icons/'window.rgba'
    if write_window and not window.exists():window.write_bytes(pixels)
    if not window.is_file() or window.read_bytes()!=pixels:
        raise ValueError('Window pixels must exactly match the approved32px ICO frame')
    return {'brand':'v1-door','ico_sha256':ICO_SHA256,'ico_sizes':sorted(w for w,h in frames),
            'tray_size':[16,16],'tray_sha256':TRAY_SHA256,'window_size':[32,32],
            'window_sha256':hashlib.sha256(pixels).hexdigest(),'window_exact_ico_frame':True,
            'artwork_replaced_or_resized':False}

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--write-window',action='store_true')
    args=parser.parse_args()
    print(json.dumps(verify_assets(args.source,write_window=args.write_window),indent=2))
