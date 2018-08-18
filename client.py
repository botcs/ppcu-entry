#!/usr/bin/env python3

## Generally useful libraries
# os - for manipulating paths and files like mv, rm, copy 
# time - for basic benchmark purposes
# argparse - pass arguments from the command line to the script becomes extremely useful 
# pathlib - helps finding the containing directory
import os
from time import time
import argparse
import pathlib

# base64 - helps encoding the image buffer to binary strings
# json - data is sent through as binary strings, JSON helps serializing dicts
# threading - required for receieving data asynchronously from the server
# zmq - communication with the server
import base64
import json, pickle
import threading
import zmq
# Required if socket is used in a thread
import zmq.eventloop.ioloop
zmq.eventloop.ioloop.install()

## Computer vision modules
# torch - for neural network and GPU accelerated processes
# cv2 - for capturing web-cam and displaying the live stream
# numpy - for general matrix manipulation of cv2 image arrays
import numpy as np
import dlib
import torch
import cv2

## pytorch utility functions
# FloatTensor - is set as the default Tensor type when recasting, easy to switch to half-precision
# ToTensor - takes a numpy array and converts it to a torch array while normalizing it as well
from torch.cuda import FloatTensor as Tensor
from torchvision.transforms import ToTensor

## Sam utility modules
# TODO: naming convention refactor
# these bits provide auxiliary code that implements the following:
# 
# prepareOpenFace - neural network architecture, description of the information flow graph
# AlignDlib - Preprocess steps before the face-recognition network. E.g. cropping and rotating faces
# db_query - interface to MySQL server
# ITKGatePirate - interface for communication with specific Wiegand card reader hardware
# drawBBox, drawBanner - display decorators
# getCard2Name - connets CardID to userID for display
# initDB - initialize the MySQL Database

import utils
from utils import prepareOpenFace
from utils import AlignDlib
from utils import rect_to_bb
from utils import db_query
from utils import ITKGatePirate
from utils import drawBBox, drawBanner
from utils import CardValidationTracer, PredictionTracer
from utils import getCard2Name, initDB


# Knowing where the script is running can be really helpful for setting proper defaults
containing_dir = str(pathlib.Path(__file__).resolve().parent)
fileDir = os.path.dirname(os.path.realpath(__file__))
modelDir = os.path.join(fileDir, 'weights')

## Define parameters that can be modified externally
# Routes to essential runtime weights
# Authorization: same name appears in the top K match above the T treshold ratio for C consecutive frames
# Displayed application parameters
## Routes
parser = argparse.ArgumentParser()
parser.add_argument('--embedding-weights', type=str, help='Path to embedding network weights',
                    default=os.path.join(modelDir, 'openface.pth'))
parser.add_argument('--database', type=str, help='path to embedding->name database',
                    default=os.path.join(modelDir, 'REALTIME-DB.tar'))
parser.add_argument('--dlib-face-predictor', type=str, help='Path to dlib\'s face predictor.',
                    default=os.path.join(modelDir, 'shape_predictor_68_face_landmarks.dat'))


## Auth
parser.add_argument('--consecutive', type=int, default=30, 
    help='How many frames is required to be authorized as the same person')
parser.add_argument('--k', type=int, help='List top K results', default=100)
parser.add_argument('--threshold', type=int, help='Threshold for opening count in %%', default=50)

## Display
parser.add_argument('--region', type=int, nargs=4, help='detect face only in [Xmin Ymin Width Height] region')
parser.add_argument('--display', action='store_true', help='Use OpenCV to show predictions on X')
parser.add_argument('--fullscreen', action='store_true', help='Enable Full Screen display. Only available if --display is used')
parser.add_argument('--card-cooldown', type=int, help='Disable card writer for N secs after each attempt to write', default=3)
parser.add_argument('--virtual', action='store_true', help='Disable card reader')
parser.add_argument('--cam', type=int, default=0, help='Specify video stream /dev/video<cam> to use')
parser.add_argument('--server-address', default='tcp://localhost:5555', help='Where to send raw image and card reader data, and receive statistics from. Default: "tcp://198.159.190.163:5555"')
args = parser.parse_args()
print('Args parsed:', args)
    

def initializeClient():
    global IS_CLIENT_RUNNING
    global start_time
    global cap
    global it
    global idle_begin
    global pirate
    global client_socket
    
    # These will be updated if server sends new data
    global id_counter
    global BOUNDING_BOXES
    global MAIN_BBOX
    global CARD2NAME
    global OPEN_GATE
    global AUTHORIZED_ID
    global RECOGNIZED_ID
    global consecutive_occurrence
    
    
    # Initialize webcam before loading every other module
    cap = cv2.VideoCapture(args.cam)
    ret, _ = cap.read()
    if not ret:
        raise RuntimeError('Video capture was unsuccessful.')
    
    IS_CLIENT_RUNNING = True
    start_time = time()
    it = 0
    idle_begin = -1
    
    if not args.virtual:
        pirate = ITKGatePirate() 
        
    if args.display:
        cv2.namedWindow('frame', cv2.WINDOW_NORMAL)
        if args.fullscreen:
            cv2.namedWindow('frame', cv2.WND_PROP_FULLSCREEN)
            cv2.setWindowProperty(
                'frame',cv2.WND_PROP_FULLSCREEN,cv2.WINDOW_FULLSCREEN)

    
    context = zmq.Context()
    client_socket = context.socket(zmq.PAIR)
    client_socket.connect(args.server_address)
    client_socket.RCVTIMEO = 1000 # in milliseconds
    
    id_counter = None
    BOUNDING_BOXES = None
    MAIN_BBOX = None
    CARD2NAME = {}
    OPEN_GATE = False
    AUTHORIZED_ID = None
    RECOGNIZED_ID = None
    consecutive_occurrence = 0
    
    
    
def send(bgrImg, AUTHORIZED_ID):
    #imgString = cv2.imencode('.jpg', bgrImg)[1].tostring()
    
    encoded, buffer = cv2.imencode('.jpg', bgrImg)
    jpg_as_text = base64.b64encode(buffer)
    
    client_data = {
        'bgrImg': jpg_as_text,
        'AUTHORIZED_ID': AUTHORIZED_ID
    }
    message = pickle.dumps(client_data)
    lock = threading.RLock()
    #lock.acquire()
    try:
        client_socket.send(message)
    except RuntimeError as e:
        print('CLIENT <send> ERROR: ', e)
        
    finally:
        pass
        #lock.release()
    print('Send image and ID', bgrImg.shape, AUTHORIZED_ID)


def recv():
    '''
    Returns or updates:
      - id_counter: sorted list of (card_id, #occurrence_in_query)
      - BOUNDING_BOXES:       dlib.rectangles of all detected faces
      - MAIN_BBOX:  dlib.rectangle on the closest face
      - CARD2NAME:  dict that maps card_id to shibboleth_id
      - OPEN_GATE:  Boolean, if true client open the registered gate
      - AUTHORIZED_ID: if tracker can trace ID it will be used
      - RECOGNIZED_ID: final suggestion of the face recog. service
      - consecutive_occurrence: # of times RECOGNIZED_ID being the top1
    '''
    global id_counter
    global BOUNDING_BOXES
    global MAIN_BBOX
    global CARD2NAME
    global OPEN_GATE
    global AUTHORIZED_ID
    global RECOGNIZED_ID
    global consecutive_occurrence
    
    lock = threading.RLock()
    #lock.acquire()
    try:
        message = client_socket.recv_string()
        statistics = json.loads(message)
    
        id_counter = statistics['id_counter']
        BOUNDING_BOXES = statistics['BOUNDING_BOXES']
        MAIN_BBOX = statistics['MAIN_BBOX']
        CARD2NAME = statistics['CARD2NAME']
        OPEN_GATE = statistics['OPEN_GATE']
        AUTHORIZED_ID = statistics['AUTHORIZED_ID']
        RECOGNIZED_ID = statistics['RECOGNIZED_ID']
        consecutive_occurrence = statistics['consecutive_occurrence']
        
    except RuntimeError as e:
        print('CLIENT <recv> ERROR: ', e)
    finally:
        pass
        #lock.release()

    


def asyncRecvLoop():
    max_timeout = 100
    current_timeout = 0
    while IS_CLIENT_RUNNING and current_timeout < max_timeout:
        try:
            recv()
        except zmq.Again as e:
            current_timeout += 1
            print('CLIENT <recv> TIMEOUT, retries: [%3d/%3d]'%(
                current_timeout, max_timeout))
            
        except RuntimeError as e:
            print('CLIENT <recv loop> ERROR: ', e)
        except KeyboardInterrupt:
            print('\nInterrupted manually')
            break
        
    print('exiting async recv loop')



if __name__ == '__main__':
    initializeClient()
    recvThread = threading.Thread(name='<recv loop thread>', target=asyncRecvLoop)
    recvThread.start()

    # Initializing the face recognition application parameters
    #last_cardwrite = time()
    #consecutive_occurrence = 0
    #torch.no_grad().__enter__()
    print('Begin capture')
    while IS_CLIENT_RUNNING:
        it += 1
        
        #cardTracer.flush()
        #predTracer.flush()

        try:
            # STEP 1: READ IMAGE
            ret, bgrImg = cap.read()
            bgrImg = cv2.flip(bgrImg, 1)
            if not ret:
                raise RuntimeError('Video capture was unsuccessful.')
                
            # STEP 2: READ CARD                
            if not args.virtual:
                CardData = pirate.readCardID(max_age=1000)
                
            if AUTHORIZED_ID is None:
                # HERE COMES THE CARD ID
                if args.virtual:
                    # USE KEY PRESS AS AUTHORIZATION, ID WILL BE THE CHARACTER PRESSED
                    pressedKeyCode = cv2.waitKey(10)
                    if pressedKeyCode != -1:
                        AUTHORIZED_ID = chr(pressedKeyCode & 255)
                else:
                    if len(CardData) == 4:                    
                        AUTHORIZED_ID = CardData[0]
                
            # TODO: Send the frame and AUTHORIZED_ID to server
            threading.Thread(
                target=send, 
                args=(bgrImg.copy(), AUTHORIZED_ID)
            ).start()
            
            # TODO: Receieve stats from server
            # - id_counter: sorted list of (card_id, #occurrence_in_query)
            # - BBOX:       dlib.rectangles of all detected faces
            # - MAIN_BBOX:  dlib.rectangle on the closest face
            # - CARD2NAME:  dict that maps card_id to shibboleth_id
            # - OPEN_GATE:  Boolean, if true client open the registered gate
            # - AUTHORIZED_ID: if tracker can trace ID it will be used
            # - RECOGNIZED_ID: final suggestion of the face recog. service
            # - consecutive_occurrence: # of times RECOGNIZED_ID being the top1
            
            if OPEN_GATE:
                print('OPEN:', CARD2NAME[RECOGNIZED_ID], RECOGNIZED_ID, time())
                if not args.virtual:
                    pirate.emulateCardID(RECOGNIZED_ID)


            if MAIN_BBOX is None:
                if idle_begin < 0: 
                    idle_begin = time()
                idle_time = time() - idle_begin
                FPS = it / (time()-start_time)
                #print('\t\t\tZzzzzz... No face detected (%4.0f sec), FPS:%2.2f\r' %\
                #    (idle_time, FPS), flush=True, end='')
                                    
                if args.display:
                    '''
                    if args.region is not None:
                        # Draw region
                        topleft = (aligner.regionXmin, aligner.regionYmin)
                        bottomright = (aligner.regionXmax, aligner.regionYmax)
                        cv2.rectangle(bgrImg, topleft, bottomright, (255, 255, 255), 3)
                    ''' 
                    bgrImg = drawBanner(bgrImg)
                    cv2.imshow('frame', bgrImg)
                    if cv2.waitKey(10) & 0xFF == ord('q'):
                        break     

                continue                
            idle_begin = -1

            # STEP 7: IF X IS AVAILABLE THEN SHOW FACE BOXES
            if args.display:
                
                # Draw the main bounding box
                for BBOX in BOUNDING_BOXES:
                    if BBOX == MAIN_BBOX:
                        drawBBox(bgrImg, BBOX, args, id_counter, consecutive_occurrence, CARD2NAME)
                    else:
                        drawBBox(bgrImg, BBOX, args)
                
                bgrImg = drawBanner(bgrImg, id_counter, CARD2NAME, AUTHORIZED_ID)
                cv2.imshow('frame', bgrImg)
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    break

        except KeyboardInterrupt:
            cv2.destroyAllWindows()
            cap.release()
            print('\nInterrupted manually')
            IS_CLIENT_RUNNING = False
        
    # FINALLY: Save the learned representations
    # torch.save(KNOWN_DB, os.path.join(modelDir, 'REALTIME-DB.tar'))
    
        
            