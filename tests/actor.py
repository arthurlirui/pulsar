from time import sleep
import unittest as test

import pulsar


def sleepfunc():
    sleep(2)
    

class TestActorThread(test.TestCase):
    impl = 'thread'
    
    def spawn(self, **kwargs):
        arbiter = pulsar.arbiter()
        self.a = pulsar.spawn(**kwargs)
        yield pulsar.NOT_DONE
        yield self.a.on_address
        self.assertTrue(self.a.aid in arbiter.MANAGED_ACTORS)
    
    def stop(self):
        arbiter = pulsar.arbiter()
        a = self.a
        yield a.send(arbiter,'stop')
        while a.aid in arbiter.MANAGED_ACTORS:
            yield pulsar.NOT_DONE
        self.assertFalse(a.is_alive())
        self.assertFalse(a.aid in arbiter.MANAGED_ACTORS)
        
    def testStartStop(self):
        '''Test start and stop for a standard actor'''
        yield self.spawn(impl = self.impl)
        a = self.a
        self.assertTrue(isinstance(a,pulsar.ActorProxy))
        self.assertTrue(a.is_alive())
        self.assertEqual(a.impl.impl,self.impl)
        yield self.stop()
    testStartStop.run_on_arbiter = True
        
    def __testStartStopQueue(self):
        '''Test start and stop for an actor using a I/O queue'''
        ioqueue = pulsar.Queue()
        yield self.spawn(impl = self.impl, ioqueue = ioqueue)
        a = self.a
        self.assertTrue(isinstance(a,pulsar.ActorProxy))
        self.assertTrue(a.is_alive())
        self.assertEqual(a.impl.impl,self.impl)
        yield self.stop()
    #testStartStopQueue.run_on_arbiter = True
    
    def testPing(self):
        arbiter = pulsar.arbiter()
        yield self.spawn(impl = self.impl)
        r,outcome = pulsar.async_pair(self.a.send(arbiter,'ping'))
        yield r
        self.assertEqual(outcome.result,'pong')
        self.assertFalse(r.rid in pulsar.ActorMessage.MESSAGES)
        yield self.stop()
    testPing.run_on_arbiter = True
        
    def __testInfo(self):
        a = spawn(Actor, impl = self.impl)
        cbk = self.Callback()
        r = self.arbiter.proxy.info(a).add_callback(cbk)
        self.wait(lambda : not hasattr(cbk,'result'))
        self.assertFalse(r.rid in ActorRequest.REQUESTS)
        info = cbk.result
        self.assertEqual(info['aid'],a.aid)
        self.assertEqual(info['pid'],a.pid)
        self.stop(a)
        
    def __testSpawnFew(self):
        actors = (spawn(Actor, impl = self.impl) for i in range(5))
        for a in actors:
            self.assertTrue(a.aid in self.arbiter.LIVE_ACTORS)
            cbk = self.Callback()
            r = self.arbiter.proxy.ping(a).add_callback(cbk)
            self.wait(lambda : not hasattr(cbk,'result'))
            self.assertEqual(cbk.result,'pong')
            self.assertFalse(r.rid in ActorRequest.REQUESTS)
                
    def __testTimeout(self):
        a = spawn(Actor, on_task = sleepfunc, impl = self.impl, timeout = 1)
        self.assertTrue(a.aid in self.arbiter.LIVE_ACTORS)
        self.wait(lambda : a.aid in self.arbiter.LIVE_ACTORS, timeout = 3)
        self.assertFalse(a.aid in self.arbiter.LIVE_ACTORS)
        

#class TestActorProcess(TestActorThread):
#    impl = 'process'        

