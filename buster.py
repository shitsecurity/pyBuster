#!/usr/bin/env python

from __future__ import division, print_function

import os
import sys

import gevent.monkey
gevent.monkey.patch_all()

from gevent import spawn as gspawn, wait
from gevent.queue import Queue, JoinableQueue
from gevent.lock import BoundedSemaphore, RLock

import requests.packages
requests.packages.urllib3.disable_warnings()

from requests import Session
from requests.adapters import HTTPAdapter
from requests.exceptions import ConnectionError, Timeout

import random
import string

import logging
import traceback

import urlparse
import contextlib

from difflib import SequenceMatcher
from functools import partial
Diff=partial(SequenceMatcher,None)

import argparse

class Baseline( object ):

	def __init__( self, url, code, content ):
		self.url=url
		self.code=code
		self.content=content

class ReadReadyLockDict( dict ):
	'''
	Lock on access if key is not set until it is
	'''

	def __init__( self, *args, **kwargs ):
		super( ReadReadyLockDict, self ).__init__( *args, **kwargs )
		self._locks = {}
		self._mutex_r = RLock()
		self._mutex_w = RLock()

	def get( self, key, d=None ):
		with self._mutex_r:
			lock = self._locks.setdefault( key, BoundedSemaphore() )
			if not super( ReadReadyLockDict, self ).__contains__( key ):
				lock.acquire()
			return super( ReadReadyLockDict, self ).get( key, d )

	def __getitem__( self, key ):
		with self._mutex_r:
			lock = self._locks.setdefault( item, BoundedSemaphore() )
			if not super( ReadReadyLockDict, self ).__contains__( key ):
				lock.acquire()
			return super( ReadReadyLockDict, self ).__getitem__( key )

	def __contains__( self, item ):
		with self._mutex_r:
			lock = self._locks.setdefault( item, BoundedSemaphore() )
			if not super( ReadReadyLockDict, self ).__contains__( item ):
				lock.acquire()
			return super( ReadReadyLockDict, self ).__contains__( item )

	def __setitem__( self, key, value ):
		with self._mutex_w:
			result = super( ReadReadyLockDict, self ).__setitem__( key, value )
			lock = self._locks.get(key)
			if lock is not None:
				lock.release()
			return result

	def setdefault( self, key, d=None ):
		with contextlib.nested( self._mutex_w, self._mutex_r ):
			return super( ReadReadyLockDict, self ).setdefault( key, d )

	def clear( self ):
		with contextlib.nested( self._mutex_w, self._mutex_r ):
			self._locks.clear()
			super( ReadReadyLockDict, self ).clear()

	def __delitem__( self, key ):
		with contextlib.nested( self._mutex_w, self._mutex_r ):
			self._locks.__delitem__( key )
			super( ReadReadyLockDict, self ).__delitem__( key )

cache = ReadReadyLockDict()

def create_session():
	session = Session()
	adapter = HTTPAdapter( max_retries=3 )
	session.mount( 'http', adapter )
	return session

def fetch( url, session=None ):
	session = session or create_session()
	session.headers.setdefault( 'User-Agent',
								'Mozilla/5.0 (compatible; Trident/6.0)' )
	try:
		logging.debug('requesting {}'.format( url ))
		response = session.get( url, timeout=5, verify=False )
	except ConnectionError, Timeout:
		logging.error( '{}\n{}'.format( url, traceback.format_exc() ))
		return
	return response

def bust_worker( q_in, q_out, *args, **kwargs ): # session, url, diff404
	while True:
		q_out.put( bust( q_in.get(), *args, **kwargs ))
		q_in.task_done()

def result_worker( q_in, ratio, action=None ):
	while True:
		data = q_in.get()
		if(result( data, threshhold=ratio ) and action is not None):
			action( data )

def result( response, threshhold=0.95 ):
	domain = urlparse.urlparse(response.url).netloc
	baseline = cache.get(domain)
	ignore_code = baseline.code if baseline is not None else 000
	if ignore_code != 200:
		if response.status_code not in [404,
										500,
										502,
										503,
										ignore_code]:
			logging.info('{} {}'.format(response.status_code,
										response.request_url))
			return True
	else:
		ratio = Diff( baseline.content, response.content ).ratio()
		if ratio < threshhold:
			logging.info('{:.2f} {}'.format( ratio, response.request_url ))
			return True

def bust( obj, session=None, url=None, diff404=None ):
	extract_url = lambda _: _
	url = url or extract_url
	bust_url = url(obj)
	response = fetch( bust_url, session )
	response.request_url = bust_url
	parsed_url = urlparse.urlparse( response.url )
	if parsed_url.netloc not in cache:
		rand_str = lambda: ''.join([random.choice(string.letters)
									for _ in xrange(random.randrange(8,17))])
		not_found = '{}://{}/{}'.format(parsed_url.scheme,
										parsed_url.netloc,
										diff404 or rand_str())
		baseline_response = fetch( not_found, session )
		cache[parsed_url.netloc] = Baseline(url=url(obj),
											code=baseline_response.status_code,
											content=baseline_response.content)
	return response

def log( level=logging.INFO, file=None ):
	logging.basicConfig(level=level, filename=file,
						format='%(levelname)8s [*] %(message)s')
	requests_logger = logging.getLogger("requests")
	requests_logger.setLevel( logging.ERROR )

def spawn( worker, amount, *args, **kwargs ):
	return [ gspawn( worker, *args, **kwargs ) for _ in xrange( amount ) ]

def buster( urls,
			session=None,
			concurrency=10,
			ratio=0.95,
			url=None,
			action=None,
			diff404=None ):

	session = session or create_session()
	q_in = JoinableQueue(concurrency)
	q_out = Queue(concurrency)
	workers = spawn(bust_worker,
					concurrency,
					q_in,
					q_out,
					session,
					url,
					diff404)
	results = spawn( result_worker, 1, q_out, ratio, action )
	[ q_in.put( _ ) for _ in urls ]
	wait()

def urls( url, paths, exts=[], progress=False ):
	if not url.startswith('http'):
		url = 'http://{}'.format( url )
	if progress:
		total = len(paths)*(len(exts) or 1)
		print('[*] queued {}'.format( total ))
		counter=0
	for path in paths:
		for ext in exts or ['']:
			yield url.format(file=path.lstrip('/'),ext=ext)
			if progress:
				counter += 1
				if counter%128==0:
					done = int( counter*78/total )
					left = 78 - done
					print( '[' + ':'*done + ' '*left + ']' )
	if progress:
		print( '[' + ':'*78 + ']' )

def load_file( file ):
	try:
		with open( file, 'rb' ) as fh:
			return [ _.rstrip() for _ in fh.readlines() ]
	except IOError:
		print('[!] file {} cannot be opened'.format( file ))
		sys.exit()

def parse_args():
	parser = argparse.ArgumentParser(description='bust some shit')
	parser.add_argument('--file', metavar='bust.wl', dest='file', type=str,
						help='load files', default=None )
	parser.add_argument('--ext', metavar='.php', dest='exts', type=str,
						help='extensions', nargs='+', default=[] )
	parser.add_argument('--target', metavar='domain.tld/{file}', dest='target',
						type=str, help='targets', nargs='+', default=[] )
	parser.add_argument('--workers', metavar='10', dest='workers', type=int,
						help='workers', default=10 )
	parser.add_argument('--verbose', action='store_true', dest='verbose',
						help='be verbose', default=False )
	parser.add_argument('--log', metavar='bust.log', dest='log', type=str,
						help='log to file', default=None )
	parser.add_argument('--progress', action='store_true', dest='progress',
						help='show progress', default=False )
	parser.add_argument('--ratio', metavar='0.95', dest='ratio', type=float,
						help='diff ratio', default=0.95 )
	parser.add_argument('--diff', metavar='404_NOT_FOUND', dest='diff',
						type=str, help='custom 404 path', default=None )
	args = parser.parse_args()

	if not args.file:
		parser.error('select wordlist')

	if not args.target:
		parser.error('select target')

	return args

if __name__ == "__main__":
	args = parse_args()

	if args.verbose:
		level = logging.DEBUG
	else:
		level = logging.INFO

	if args.log and os.path.exists(args.log):
		os.remove(args.log)
		
	log( level=level, file=args.log )

	paths = load_file( args.file )
	for target in args.target:
		buster( urls( target, paths, args.exts, progress=args.progress ),
				ratio=args.ratio,
				concurrency=args.workers,
				diff404=args.diff )
		cache.clear()
