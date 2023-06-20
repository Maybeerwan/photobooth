#!/usr/bin/env python

import argparse
import os
import io
import psutil
import signal
import subprocess
import sys
import time
import zmq
from argparse import Namespace
from subprocess import Popen, PIPE
from datetime import datetime, timedelta

from picamera2 import Picamera2
from picamera2.outputs import FileOutput


TEMP_VIDEO_FILE_APPENDIX = '.temp.mp4'


class picamcontrol:
    def __init__(self, args):
        self.running = True
        self.args = args
        self.showVideo = True
        self.chroma = {}
        self.picam = None
        self.socket = None
        self.ffmpeg = None
        self.bsm_stopTime = None
        self.picam = Picamera2()
        self.picamCaptureConfig = None
        self.picamPreviewConfig = None

        signal.signal(signal.SIGINT, self.exit_gracefully)
        signal.signal(signal.SIGTERM, self.exit_gracefully)

        self.connect_to_camera()

        if args.imgpath is not None:
            try:
                self.capture_image(args.imgpath)
                if args.chroma_sensitivity is not None and args.chroma_sensitivity > 0:
                   self.handle_chroma_params(args)
                   self.chroma_key_image(args.imgpath)
                   sys.exit(0)
            except RuntimeError as e:
                print('An error occured: %s' % e)
                sys.exit(1)
        else:
            self.pipe_video_to_ffmpeg_and_wait_for_commands()

    def connect_to_camera(self):
        main = {}
        if self.args.config is not None:
            print('Setting config %s' % self.args.config)
            for c in self.args.config:
                cs = c.split("=")
                if len(cs) == 2:
                    main[cs[0]] = cs[1]
                else:
                    print('Invalid config value %s' % c)     
        
        try:
            if self.picam.started:
                self.picam.stop()
                
            self.picamCaptureConfig = self.picam.create_still_configuration(main=main, raw={})
            self.picamPreviewConfig = self.picam.create_preview_configuration()
            self.picam.configure(self.picamPreviewConfig)
            self.picam.start()
            print('Connected to camera')
        except RuntimeError as e:
            print('An error occured: %s' % e)
            pass

    def capture_image(self, path):
        self.picam.switch_mode_and_capture_file(self.picamCaptureConfig , path)

    def print_config(self, name):
        print(self.picamCaptureConfig)

    def disable_video(self):
        self.bsm_stopTime = None
        self.showVideo = False
        self.picam.stop()
        print('Video disabled')

    def handle_message(self, message):
        args = Namespace(**message)
        if args.exit:
            self.socket.send_string('Exiting service!')
            self.exit_gracefully()
        video_settings_were_updated = self.handle_chroma_params(args)
        video_settings_were_updated = video_settings_were_updated or self.handle_video_params(args)
        self.handle_bsm_timeout(args)
        if args.config is not None and args.config != self.args.config:
            self.args.config = args.config
            self.connect_to_camera()
            video_settings_were_updated = True
            print('Applied updated config')
        if args.device != self.args.device:
            self.args.device = args.device
            video_settings_were_updated = True
            print('Video output device changed')
        if video_settings_were_updated:
            self.ffmpeg_open()
            print('Restarted ffmpeg stream with updated video settings')
        if args.imgpath is not None:
            self.capture_image(args.imgpath)
            if args.chroma_sensitivity is not None and args.chroma_sensitivity > 0:
                self.chroma_key_image(args.imgpath)
            self.socket.send_string('Image captured')
            if self.args.bsm:
                self.disable_video()
        else:
            self.args.bsm = args.bsm
            if not self.showVideo and not args.bsmx:
                self.showVideo = True
                self.connect_to_camera()
                self.socket.send_string('Starting Video')
            else:
                if args.bsmx:
                    self.socket.send_string('Updated config. Video not starting because of option --bsmx')
                else:
                    self.socket.send_string('Video already running')


    def ffmpeg_open(self):
        input_config = ['-i', '-', '-vcodec', 'rawvideo', '-pix_fmt', 'yuv420p']
        stream = ['-preset', 'ultrafast', '-f', 'v4l2', self.args.device]
        pre_input = []
        filters = []
        file_output = []
        if self.chroma.get('active', False):
            filters, pre_input = self.get_chroma_ffmpeg_params()
        if self.args.video_path is not None:
            temp_video_path = self.args.video_path + TEMP_VIDEO_FILE_APPENDIX
            if os.path.exists(self.args.video_path) or os.path.exists(temp_video_path):
                print('Video recording stopped: file or temp file already exist')
            else:
                pre_input = ['-t', str(self.args.video_length)]
                file_output = ['-vf', 'fps=' + str(self.args.video_fps), temp_video_path]
                if self.args.video_frames > 0:
                    # 99 images should be more than enough
                    if self.args.video_frames > 99:
                        self.args.video_frames = 99
                    image_fps = self.args.video_frames / self.args.video_length
                    file_output.extend(['-vf', 'fps=' + str(image_fps), self.args.video_path + '-%02d.jpg'])
        commands = ['ffmpeg', *pre_input, *input_config, *filters, *stream, *file_output]
        print(commands)
        if self.ffmpeg:
            print("end open ffmpeg stream to start a new one")
            self.ffmpeg.kill()
        self.ffmpeg = Popen(commands, stdin=PIPE)

    def handle_bsm_timeout(self, args):
        if args.bsm_timeOut > 0:
            self.bsm_stopTime = datetime.now() + timedelta(minutes=args.bsm_timeOut)
            print('Set bsm stop time to ', self.bsm_stopTime.strftime("%d.%m.%Y %H:%M:%S"))
        else:
            self.bsm_stopTime = None

    def handle_chroma_params(self, args):
        chroma_color = args.chroma_color or self.chroma.get('color', '0xFFFFFF')
        chroma_image = args.chroma_image or self.chroma.get('image')
        chroma_sensitivity = float(args.chroma_sensitivity or self.chroma.get('sensitivity', 0.0))
        if chroma_sensitivity < 0.0 or chroma_sensitivity > 1.0:
            chroma_sensitivity = 0.0
        chroma_blend = float(args.chroma_blend or self.chroma.get('blend', 0.0))
        if chroma_blend < 0.0:
            chroma_blend = 0.0
        elif chroma_blend > 1.0:
            chroma_blend = 1.0
        chroma_active = chroma_sensitivity != 0.0 and chroma_image is not None
        print('chromakeying active: %s' % chroma_active)
        new_chroma = {
            'active': chroma_active,
            'image': chroma_image,
            'color': chroma_color,
            'sensitivity': str(chroma_sensitivity),
            'blend': str(chroma_blend)
        }
        settings_changed = new_chroma != self.chroma
        self.chroma = new_chroma
        return settings_changed

    def handle_video_params(self, args):
        self.args.video_path = args.video_path
        self.args.video_frames = args.video_frames
        self.args.video_length = args.video_length
        self.args.video_fps = args.video_fps
        return args.video_path is not None

    def get_chroma_ffmpeg_params(self):
        input_chroma = ['-i', self.chroma['image']]
        filters = ['-filter_complex', '[0:v][1:v]scale2ref[i][v];' +
                   '[v]colorkey=%s:%s:%s:[ck];[i][ck]overlay' %
                   (self.chroma['color'], self.chroma['sensitivity'], self.chroma['blend'])]
        return filters, input_chroma

    def chroma_key_image(self, path):
        input_chroma = []
        filters = []
        if self.chroma.get('active', False):
            filters, input_chroma = self.get_chroma_ffmpeg_params()
        input_gphoto = ['-i', path]
        tmp_path = "%s-chroma.jpg" % path
        if subprocess.run(['ffmpeg', *input_chroma, *input_gphoto, *filters, tmp_path]).returncode != 0:
            print('Chroma keying failed')
            return
        if subprocess.run(['mv', tmp_path, path, '-f']).returncode != 0:
            print('Failed to rename temporary file to file filename')

    def pipe_video_to_ffmpeg_and_wait_for_commands(self):
        print('setup serveur')
        context = zmq.Context()
        self.socket = context.socket(zmq.REP)
        self.socket.bind('tcp://*:5555')
        self.handle_chroma_params(self.args)
        self.handle_bsm_timeout(self.args)
        self.ffmpeg_open()
        print('server start')
        try:
            while True:
                try:
                    message = self.socket.recv_json(flags=zmq.NOBLOCK)
                    print('Received: %s' % message)
                    self.handle_message(message)
                except zmq.Again:
                    pass
                try:
                    if self.bsm_stopTime is not None and datetime.now() > self.bsm_stopTime:
                        print('Camera stopped because of bsm stop time')
                        self.disable_video()
                    if self.showVideo:
                        data = io.BytesIO()
                        self.picam.capture_file(data, format='jpeg')
                        self.ffmpeg.stdin.write(data.getbuffer().tobytes())
                    else:
                        time.sleep(0.1)
                except RuntimeError as e:
                    time.sleep(1)
                    print('Not connected to camera : (%s). Trying to reconnect...' % e)
                    self.connect_to_camera()
                except BrokenPipeError:
                    print('Broken pipe: check if video recording finished, restart ffmpeg')
                    if self.args.video_path is not None:
                        temp_video_path = self.args.video_path + TEMP_VIDEO_FILE_APPENDIX
                        if os.path.exists(temp_video_path):
                            print('Video recording successful')
                            os.rename(temp_video_path, self.args.video_path)
                            self.args.video_path = None
                        else:
                            print('Video recording failed. Restart camera connection and retry.')
                            self.picam.stop_recording()
                            self.connect_to_camera()
                    else:
                        print('No video recording. Restart camera connection')
                        self.picam.stop()
                        self.connect_to_camera()
                    self.ffmpeg_open()
        except KeyboardInterrupt:
            self.exit_gracefully()

    def exit_gracefully(self, *_):
        if self.running:
            self.running = False
            print('Exiting...')
            if self.picam:
                self.disable_video()
                print('Closed camera connection')
            sys.exit(0)


class MessageSender:
    def __init__(self, message):
        try:
            context = zmq.Context()
            socket = context.socket(zmq.REQ)
            socket.setsockopt(zmq.RCVTIMEO, 10000)
            socket.connect('tcp://localhost:5555')
            print('Sending message: %s' % message)
            socket.send_json(message)
            response = socket.recv_string()
            print(response)
            if response == 'failure':
                sys.exit(1)
        except zmq.Again:
            print('Message receival not confirmed')
            sys.exit(1)
        except KeyboardInterrupt:
            print('Interrupted!')


def get_running_pid():
    for p in psutil.process_iter(['name', 'cmdline']):
        if p.name() == 'python3' and p.cmdline()[1].endswith('picamcontrol.py') and p.pid != os.getpid():
            return p.pid
    return -1


def main():
    parser = argparse.ArgumentParser(description='Simple picamera Control script using picamera2 and \
    libcamera.', epilog='you should configure your camera to capture only jpeg images. For RAW+JPEG: this is possible but not test',
                                     allow_abbrev=False)
    parser.add_argument('-d', '--device', nargs='?', default='/dev/video0',
                        help='virtual device the ffmpeg stream is sent to')
    parser.add_argument('-s', '--set-config', action='append', default=None, dest='config',
                        help='CONFIGENTRY=CONFIGVALUE. only "size" and "format" for capture. Not tested')
    parser.add_argument('-c', '--capture-image-and-download', default=None, type=str, dest='imgpath',
                        help='capture an image and download it to the computer. If it stays stored on the camera as \
                        well depends on the camera config. If this param is set while the service is not already \
                        running the application will take a single image and exit after that. Chroma params are used \
                        for that image, but no video will be created')
    parser.add_argument('-b', '--bsm', action='store_true', help='start preview, but quit preview after taking an \
                        image and wait for message to start preview again', dest='bsm')
    parser.add_argument('--bsmx', action='store_true', help='In bsm mode: prevent picamcontrol.py from restarting \
                        the video. Useful to just execute a command', dest='bsmx')
    parser.add_argument('--bsmtime', default=0, type=int, help='Keep preview active for the specified \
                        time in minutes before ending the preview video. Set to 0 to disable', dest='bsm_timeOut')
    parser.add_argument('-v', '--video', default=None, type=str, dest='video_path',
                        help='save the next part of the preview as a video file')
    parser.add_argument('--vframes', default=4, type=int, help='saves shots from the video in an equidistant time',
                        dest='video_frames')
    parser.add_argument('--vlen', default=3, type=int, help='duration of the video in seconds',
                        dest='video_length')
    parser.add_argument('--vfps', default=10, type=int, help='fps of the video',
                        dest='video_fps')
    parser.add_argument('--chromaImage', type=str, help='chroma key background (full path)', dest='chroma_image')
    parser.add_argument('--chromaColor', type=str,
                        help='chroma key color (color name or format like "0xFFFFFF" for white)', dest='chroma_color')
    parser.add_argument('--chromaSensitivity', type=float,
                        help='chroma key sensitivity (value from 0.01 to 1.0 or 0.0 to disable). \
                             If this is set to a value distinct from 0.0 on capture image command chroma keying using \
                             ffmpeg is applied on the image and only this modified image is stored on the pc. \
                             If this is set on a preview command you get actual live chroma keying',
                        dest='chroma_sensitivity')
    parser.add_argument('--chromaBlend', type=float, help='chroma key blend (0.0 to 1.0)', dest='chroma_blend')
    parser.add_argument('--exit', action='store_true', help='exit the service')

    args = parser.parse_args()
    pid = get_running_pid()
    if pid > 0:
        print("Service running with pid %d" % pid)
        MessageSender(vars(args))
    else:
        picamcontrol(args)


if __name__ == '__main__':
    main()
