"""One-document, memory-only OCR worker. Invoked only by local_ocr.py."""
import io
import json
import math
import os
from pathlib import Path
import sys


MAX_PAGES = 20
MAX_PIXELS = 12_000_000
MAX_OUTPUT = 4 * 1024 * 1024


def reading_order(regions):
    """Group overlapping line centers, then left-to-right; not table semantics."""
    def bounds(r):
        ys=[p[1] for p in r['quad']]
        return (min(ys)+max(ys))/2,max(max(ys)-min(ys),.001),min(p[0] for p in r['quad'])
    lines=[]
    for region in sorted(regions,key=lambda r:bounds(r)[0]):
        cy,height,_=bounds(region)
        target=next((line for line in reversed(lines) if abs(cy-line[0]) <= .5*min(height,line[1])),None)
        if target:target[2].append(region)
        else:lines.append([cy,height,[region]])
    return [r for _,_,line in lines for r in sorted(line,key=lambda r:bounds(r)[2])]


def emit(value):
    sys.stdout.write(json.dumps(value, ensure_ascii=True, separators=(',', ':')) + '\n')
    sys.stdout.flush()


def offline_memory_only(event, args):
    if event in {'socket.connect', 'socket.getaddrinfo', 'socket.bind', 'socket.sendto'}:
        raise PermissionError('OCR runtime network disabled')
    if event == 'open':
        _, mode, flags = args
        if (isinstance(mode, str) and any(c in mode for c in 'wax+')) or (isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)):
            raise PermissionError('OCR runtime filesystem writes disabled')
    if event in {'os.remove', 'os.rename', 'os.mkdir', 'subprocess.Popen', 'os.system'}:
        raise PermissionError('OCR runtime side effect disabled')


def main():
    # Parent assigns the Windows memory/kill Job before sending this header.
    header = json.loads(sys.stdin.buffer.readline(8192))
    size = int(header['size'])
    if not 0 < size <= 16 * 1024 * 1024: raise ValueError('input_limit')
    data = sys.stdin.buffer.read(size)
    if len(data) != size: raise ValueError('incomplete_input')
    sys.addaudithook(offline_memory_only)
    import numpy as np
    from PIL import Image, ImageDraw, ImageStat
    import pypdfium2 as pdfium
    if header.get('preview_page') is not None:
        import base64
        number=int(header['preview_page'])
        if not 1 <= number <= MAX_PAGES:raise ValueError('page_limit')
        if header['suffix']=='.pdf':
            with pdfium.PdfDocument(data) as document:
                if len(document)>MAX_PAGES or number>len(document):raise ValueError('page_limit')
                page=document[number-1];width,height=page.get_size()
                if not (0<width<=14400 and 0<height<=14400):raise ValueError('input_limit')
                bitmap=page.render(scale=min(200/72,2000/max(width,height)))
                image=bitmap.to_pil().convert('RGB');bitmap.close();page.close();total=len(document)
        else:
            with Image.open(io.BytesIO(data)) as original:
                total=getattr(original,'n_frames',1)
                if total>MAX_PAGES or number>total:raise ValueError('page_limit')
                original.seek(number-1)
                if original.width*original.height>MAX_PIXELS:raise ValueError('input_limit')
                image=original.convert('RGB');image.thumbnail((2000,2000))
        output=io.BytesIO();image.save(output,format='PNG');payload=output.getvalue()
        if len(payload)>1024*1024:raise ValueError('input_limit')
        emit({'kind':'preview','page':number,'pages_total':total,'image_base64':base64.b64encode(payload).decode(),
              'mime_type':'image/png','width':image.width,'height':image.height,'preview_only':True})
        return
    import onnxruntime as ort
    ort.disable_telemetry_events()
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    models = Path(header['models'])
    engine = None

    def recognize(image):
        nonlocal engine
        if engine is None:
            emit({'kind': 'stage', 'stage': 'ocr_loading'})
            from rapidocr import RapidOCR
            engine = RapidOCR(params={
                'Det.model_path': str(models / 'PP-OCRv6_det_small.onnx'),
                'Rec.model_path': str(models / 'PP-OCRv6_rec_small.onnx'),
                'Cls.model_path': str(models / 'ch_ppocr_mobile_v2.0_cls_mobile.onnx'),
                'Global.log_level': 'critical', 'Global.text_score': 0.0,
                'Global.max_side_len': 2400,
                'EngineConfig.onnxruntime.intra_op_num_threads': 2,
                'EngineConfig.onnxruntime.inter_op_num_threads': 1,
                'EngineConfig.onnxruntime.use_cuda': False,
                'EngineConfig.onnxruntime.use_dml': False,
                'Rec.rec_batch_num': 2, 'Cls.cls_batch_num': 2,
            })
        # ndarray is BGR, no filename/URL and no visualization/save API.
        result = engine(np.asarray(image.convert('RGB'))[:, :, ::-1].copy())
        regions = []
        if result.txts is not None:
            for text, score, box in zip(result.txts, result.scores, result.boxes):
                points = [[round(float(x) / image.width, 6), round(float(y) / image.height, 6)] for x, y in box]
                review_reason='symbol_only_candidate' if not any(c.isalnum() for c in text) else 'low_engine_score' if float(score)<.85 else None
                regions.append({'text': text, 'score': float(score), 'quad': points, 'method': 'ocr',
                                'review_reason':review_reason})
        return regions

    document = None; source_image = None
    if header['suffix'] == '.pdf':
        document = pdfium.PdfDocument(data)
        total = len(document)
    else:
        source_image = Image.open(io.BytesIO(data))
        total = getattr(source_image, 'n_frames', 1)
    emit({'kind': 'stage', 'stage': 'ocr_pages', 'pages_total': total, 'pages_completed': 0})
    if total > MAX_PAGES: raise ValueError('page_limit')
    output_size = 0
    try:
        for index in range(total):
            emit({'kind': 'stage', 'stage': 'ocr_page', 'page': index + 1,
                  'pages_total': total, 'pages_completed': index})
            page = textpage = bitmap = None
            try:
                native = []; needs_ocr = True; image = None
                if document is not None:
                    page = document[index]; width, height = page.get_size()
                    if not (0 < width <= 14400 and 0 < height <= 14400): raise ValueError('page_dimensions')
                    if page.get_rotation() != 0: raise ValueError('rotated_pdf_page_requires_review')
                    textpage = page.get_textpage()
                    if textpage.count_chars() > 100000: raise ValueError('page_text_limit')
                    for rect_index in range(textpage.count_rects()):
                        rect = textpage.get_rect(rect_index)
                        text = textpage.get_text_bounded(*rect).strip()
                        if text:
                            l, b, r, t = rect
                            native.append({'text': text, 'score': None, 'method': 'native_text',
                                'quad': [[l/width, 1-t/height], [r/width, 1-t/height],
                                         [r/width, 1-b/height], [l/width, 1-b/height]]})
                    has_images = any(obj.type == pdfium.raw.FPDF_PAGEOBJ_IMAGE for obj in page.get_objects(max_depth=4))
                    needs_ocr = has_images or not native
                    if needs_ocr:
                        scale = min(200/72, 2400/max(width, height))
                        if math.ceil(width*scale)*math.ceil(height*scale) > MAX_PIXELS: raise ValueError('pixel_limit')
                        bitmap = page.render(scale=scale); image = bitmap.to_pil().convert('RGB')
                        # Preserve extractable text; mask its real rectangles before
                        # recognizing the remaining page to prevent duplicate text.
                        draw = ImageDraw.Draw(image)
                        for region in native:
                            q = region['quad']; draw.rectangle((max(0,q[0][0]*image.width-2), max(0,q[0][1]*image.height-2),
                                min(image.width,q[2][0]*image.width+2), min(image.height,q[2][1]*image.height+2)), fill='white')
                else:
                    source_image.seek(index)
                    if source_image.width*source_image.height > MAX_PIXELS: raise ValueError('pixel_limit')
                    image = source_image.convert('RGB')
                    if max(image.size) > 2400: image.thumbnail((2400,2400))
                blank = image is not None and max(ImageStat.Stat(image.convert('L')).stddev) < 1
                page_error=None
                try:ocr = recognize(image) if needs_ocr and not blank else []
                except Exception as exc:ocr=[];page_error=type(exc).__name__
                regions = reading_order(native + ocr)
                status = ('partial' if regions else 'failed') if page_error else 'recognized' if regions else 'blank' if blank else 'unrecognized'
                if status=='recognized' and any(r.get('review_reason') for r in regions):status='needs_review'
                value = {'kind': 'page', 'page': index+1, 'status': status,
                    'coordinate_space': 'normalized_top_left', 'regions': regions,
                    'ocr_used': needs_ocr and not blank, 'low_score_regions': sum(r['score'] is not None and r['score'] < .85 for r in regions),
                    'review_regions':sum(bool(r.get('review_reason')) for r in regions)}
                if page_error:value['reason']=page_error
                output_size += len(json.dumps(value, ensure_ascii=False).encode('utf-8'))
                if output_size > MAX_OUTPUT: raise ValueError('output_limit')
                emit(value)
            except Exception as exc:
                reason = str(exc) if isinstance(exc, ValueError) and str(exc) in {'pixel_limit','page_dimensions','page_text_limit','output_limit','rotated_pdf_page_requires_review'} else type(exc).__name__
                emit({'kind': 'page', 'page': index+1, 'status': 'failed', 'reason': reason, 'regions': []})
                if reason == 'output_limit': break
            finally:
                if textpage is not None: textpage.close()
                if bitmap is not None: bitmap.close()
                if page is not None: page.close()
        emit({'kind': 'done', 'pages_total': total})
    finally:
        if document is not None: document.close()
        if source_image is not None: source_image.close()


if __name__ == '__main__':
    try: main()
    except Exception as exc:
        reason = str(exc) if isinstance(exc, ValueError) and str(exc) in {'page_limit','input_limit','incomplete_input'} else type(exc).__name__
        emit({'kind': 'error', 'reason': reason})
