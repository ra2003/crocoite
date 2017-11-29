# Copyright (c) 2017 crocoite contributors
# 
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
# 
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
# 
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

"""
Classes writing data to WARC files
"""

import logging
import json
from .browser import SiteLoader
from . import packageUrl
from http.server import BaseHTTPRequestHandler
from base64 import b64decode
from io import BytesIO
from warcio.statusandheaders import StatusAndHeaders
from urllib.parse import urlsplit
from logging.handlers import BufferingHandler
import pychrome

class WARCLogHandler (BufferingHandler):
    """
    Buffered log handler, flushing to warcio
    """

    contentType = 'text/plain; charset=utf-8'

    def __init__ (self, capacity, warcfile):
        BufferingHandler.__init__ (self, capacity)
        self.warcfile = warcfile

    def flush (self):
        self.acquire ()
        try:
            if self.buffer:
                buf = ''
                for record in self.buffer:
                    buf += self.format (record)
                    buf += '\n'
                # XXX: record type?
                record = self.warcfile.create_warc_record (
                        packageUrl ('log'), 'metadata',
                        payload=BytesIO (buf.encode ('utf8')),
                        warc_headers_dict={'Content-Type': self.contentType})
                self.warcfile.write_record(record)
                self.buffer = []
        finally:
            self.release ()

class WarcLoader (SiteLoader):
    def __init__ (self, browser, url, writer,
            logger=logging.getLogger(__name__), logBuffer=1000,
            maxBodySize=10*1024*1024):
        SiteLoader.__init__ (self, browser, url, logger)
        self.writer = writer
        self.maxBodySize = maxBodySize
        self.warcLogger = WARCLogHandler (logBuffer, writer)
        self.logger.addHandler (self.warcLogger)

    def __exit__ (self, exc_type, exc_value, traceback):
        self.logger.removeHandler (self.warcLogger)
        self.warcLogger.flush ()
        return SiteLoader.__exit__ (self, exc_type, exc_value, traceback)

    @staticmethod
    def getStatusText (response):
        text = response.get ('statusText')
        if text:
            return text
        text = BaseHTTPRequestHandler.responses.get (response['status'])
        if text:
            return text[0]
        return 'No status text available'

    def loadingFinished (self, item, redirect=False):
        writer = self.writer

        req = item.request
        reqId = item.id
        resp = item.response
        url = urlsplit (resp['url'])

        # overwrite request headers with those actually sent
        newReqHeaders = resp.get ('requestHeaders')
        if newReqHeaders:
            req['headers'] = newReqHeaders

        postData = req.get ('postData')
        if postData:
            postData = BytesIO (postData.encode ('utf8'))
        path = url.path
        if url.query:
            path += '?' + url.query
        httpHeaders = StatusAndHeaders('{} {} HTTP/1.1'.format (req['method'], path),
                req['headers'], protocol='HTTP/1.1', is_http_request=True)
        initiator = item.initiator
        warcHeaders = {
                'X-Chrome-Initiator': json.dumps (initiator),
                }
        record = writer.create_warc_record(req['url'], 'request',
                payload=postData, http_headers=httpHeaders,
                warc_headers_dict=warcHeaders)
        writer.write_record(record)
        concurrentTo = record.rec_headers['WARC-Record-ID']

        # now the response
        warcHeaders = {
                'WARC-Concurrent-To': concurrentTo,
                'WARC-IP-Address': resp.get ('remoteIPAddress', ''),
                'X-Chrome-Protocol': resp.get ('protocol', ''),
                'X-Chrome-FromDiskCache': str (resp.get ('fromDiskCache')),
                'X-Chrome-ConnectionReused': str (resp.get ('connectionReused')),
                }

        rawBody = b''
        base64Encoded = False
        try:
            # check body size first, since we’re loading everything into memory
            if item.encodedDataLength < self.maxBodySize:
                body = self.tab.Network.getResponseBody (requestId=reqId)
                rawBody = body['body']
                base64Encoded = body['base64Encoded']
                if base64Encoded:
                    rawBody = b64decode (rawBody)
                    warcHeaders['X-Chrome-Base64Body'] = str (True)
                else:
                    rawBody = rawBody.encode ('utf8')
            else:
                self.logger.error ('body for {} too large {} vs {}'.format (reqId,
                        item.encodedDataLength, self.maxBodySize))
        except pychrome.exceptions.CallMethodException:
            self.logger.error ('no data for {} {} {}'.format (resp['url'],
                    resp['status'], reqId))

        httpHeaders = StatusAndHeaders('{} {}'.format (resp['status'],
                self.getStatusText (resp)), resp['headers'], protocol='HTTP/1.1')

        # Content is saved decompressed and decoded, remove these headers
        blacklistedHeaders = {'transfer-encoding', 'content-encoding'}
        for h in blacklistedHeaders:
            httpHeaders.remove_header (h)

        # chrome sends nothing but utf8 encoded text. Fortunately HTTP
        # headers take precedence over the document’s <meta>, thus we can
        # easily override those.
        contentType = resp.get ('mimeType')
        if contentType:
            if not base64Encoded:
                contentType += '; charset=utf-8'
            httpHeaders.replace_header ('content-type', contentType)

        httpHeaders.replace_header ('content-length', '{:d}'.format (len (rawBody)))

        record = writer.create_warc_record(resp['url'], 'response',
                warc_headers_dict=warcHeaders, payload=BytesIO (rawBody),
                http_headers=httpHeaders)
        writer.write_record(record)

