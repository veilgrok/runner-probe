import socket,struct,sys,threading,os
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.bind(('127.0.0.1',int(sys.argv[1])));dest=('127.0.0.1',int(sys.argv[2]));counts=[0,0]
def output():
 while True:
  d,a=s.recvfrom(65535);os.write(1,struct.pack('!H',len(d))+d);counts[0]+=1
threading.Thread(target=output,daemon=True).start()
print('UDP stdio relay ready',file=sys.stderr,flush=True)
def readn(n):
 b=b''
 while len(b)<n:
  d=os.read(0,n-len(b))
  if not d:raise EOFError()
  b+=d
 return b
try:
 while True:
  n=struct.unpack('!H',readn(2))[0];s.sendto(readn(n),dest);counts[1]+=1
except EOFError:print('relay packets',counts,file=sys.stderr,flush=True)
