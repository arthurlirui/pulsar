from __future__ import with_statement

import errno
import socket
import traceback

import pulsar

ALREADY_HANDLED = object()

class AsyncWorker(pulsar.Worker):
    '''Base class for asyncronous workers'''
    
    def timeout_ctx(self):
        raise NotImplementedError()

    def handle(self, client, addr):
        try:
            parser = http.RequestParser(client)
            try:
                while True:
                    req = None
                    with self.timeout_ctx():
                        req = parser.next()
                    if not req:
                        break
                    self.handle_request(req, client, addr)
            except StopIteration:
                pass
        except socket.error as e:
            if e[0] not in (errno.EPIPE, errno.ECONNRESET):
                self.log.exception("Socket error processing request.")
            else:
                if e[0] == errno.ECONNRESET:
                    self.log.debug("Ignoring connection reset")
                else:
                    self.log.debug("Ignoring EPIPE")
        except Exception as e:
            self.log.exception("General error processing request.")
            try:            
                # Last ditch attempt to notify the client of an error.
                mesg = "HTTP/1.0 500 Internal Server Error\r\n\r\n"
                util.write_nonblock(client, mesg)
            except:
                pass
            return
        finally:
            util.close(client)

    def handle_request(self, req, sock, addr):
        try:
            debug = self.cfg.debug or False
            self.cfg.pre_request(self, req)
            resp, environ = wsgi.create(req, sock, addr, self.address, self.cfg)
            self.nr += 1
            if self.alive and self.nr >= self.max_requests:
                self.log.info("Autorestarting worker after current request.")
                resp.force_close()
                self.alive = False
            respiter = self.wsgi(environ, resp.start_response)
            if respiter == ALREADY_HANDLED:
                return False
            for item in respiter:
                resp.write(item)
            resp.close()
            if hasattr(respiter, "close"):
                respiter.close()
            if req.should_close():
                raise StopIteration()
        except StopIteration:
            raise
        except Exception as e:
            #Only send back traceback in HTTP in debug mode.
            if not self.debug:
                raise
            util.write_error(sock, traceback.format_exc())
            return False
        finally:
            try:
                self.cfg.post_request(self, req)
            except:
                pass
        return True