import os
import struct
from io import BytesIO
from typing import Union, Dict

CACHE_DURATION = 3600
CACHE_DIRECTORY = os.path.join(os.getcwd(),"cache")

from PyCASC.utils.blizzutils import var_int,jenkins_hash,parse_build_config,parse_config,prefix_hash,hexkey_to_bytes,byteskey_to_hex
from PyCASC.utils.CASCUtils import parse_encoding_file,parse_root_file,r_cascfile,cascfile_size, NAMED_FILE,SNO_FILE,SNO_INDEXED_FILE


def prep_listfile(fp):
    names={}
    with open(fp,"r") as f:
        for x in f.readlines():
            x=x.strip()
            names[jenkins_hash(x)]=x
    return names

class FileInfo:
    ekey:int
    ckey:int
    data_file:int
    offset:int
    compressed_size:int
    uncompressed_size:int
    chunk_count:int
    name:str
    
def r_idx(fp):
    ents=[]
    with open(fp,'rb') as f:
        hl,hh,u_0,bi,u_1,ess,eos,eks,afhb,atsm,_,elen,eh=struct.unpack("IIH6BQQII",f.read(0x28))
        esize = ess+eos+eks
        for x in range(0x28,0x28+elen,esize):
            ek=var_int(f,eks,False)
            eo=var_int(f,eos,False)
            es=var_int(f,ess)
            e=FileInfo()
            e.data_file=eo>>30
            e.offset=eo&(2**30-1)
            e.compressed_size=es
            e.ekey=ek
            ents.append(e)
    return ents

def r_cidx(df): 
    # ok so my iq is incredibly high, and im going to parse cdn index files in reverse since the footer includes essential info
    d = BytesIO(df)

    curchksz=0x10
    tocCHK, u3,u2,u1, bs, eos, ess, eks, chksz, numel, ftCHK = (None,)*11
    validFooter = False
    while not validFooter and curchksz>0:
        ftrsize = curchksz*2 + 12
        d.seek(-ftrsize,2)

        tocCHK = d.read(curchksz)
        vrsn,u2,u1,bs,eos,ess,eks,chksz,numel = struct.unpack(f"8bI", d.read(12))
        ftCHK = int.from_bytes(d.read(curchksz),byteorder="little")

        # a valid footer has version 1 (that i know of), and the length of the CHKs == chksz.
        validFooter = len(tocCHK) == chksz and vrsn == 1 
        curchksz -= 1

    if not validFooter:
        raise Exception("Failed to find valid footer for cdn index file.")

    ents = {}

    dupe=0

    blk_cnt = len(df) // (bs*1024)
    max_el_per_blk = (bs*1024) // 0x18
    for x in range(blk_cnt):
        d.seek(x*bs*1024)
        for _ in range(max_el_per_blk):
            ek=var_int(d,eks,False)
            if ek in ents:
                dupe+=1
                continue

            es=var_int(d,ess)
            if ek == 0 or es == 0:
                break
            eo=var_int(d,eos)

            e=FileInfo()
            e.offset=eo
            e.compressed_size=es
            e.ekey=ek
            ents[ek] = e

    # print(f"{len(ents)} == {numel} (max is {max_el_per_blk*blk_cnt}, {dupe} dupes)")
    assert len(ents) == numel

    return ents

class CASCReader:
    ckey_map:Dict[int,int]
    file_table:Dict[int,FileInfo]
    file_translate_table:Dict[int,tuple]

    def __init__(self):
        for ckey in self.ckey_map:
            first_ekey = self.ckey_map[ckey]
            if first_ekey in self.file_table:
                fi = self.file_table[first_ekey]
                fi.ckey = ckey

        for x in self.file_translate_table:
            ckey=int(x[2],16)
            fi = self.get_file_info_by_ckey(ckey)
            if fi is None:
                continue
            if x[0] is NAMED_FILE:
                fi.name=x[1]

    def get_name(self,ckey):
        fi = self.get_file_info_by_ckey(ckey)
        if fi is not None:
            return fi.name if hasattr(fi,"name") else None
        return None

    def list_files(self):
        raise NotImplementedError()

    def list_unnamed_files(self):
        raise NotImplementedError()

    def get_file_size_by_ckey(self,ckey):
        raise NotImplementedError()

    def get_chunk_count_by_ckey(self,ckey):
        raise NotImplementedError()

    def get_file_by_ckey(self,ckey,max_size=-1):
        raise NotImplementedError()

    def get_file_info_by_ckey(self,ckey: Union[int,str]):
        raise NotImplementedError()

    def on_progress(self,step,pct):
        """ Override me! 
        This function receives progress update events for anything that takes time in the program.
        **Not implemented yet** """
        pass

from PyCASC.launcher import getProductCDNFile, getProductVersions
from PyCASC.utils.blizzutils import parse_build_config
from PyCASC.utils.CASCUtils import parse_blte
class CDNCASCReader(CASCReader):
    def __init__(self, product, region="us"):
        self.product = product

        vrs = [x for x in getProductVersions(product) if x['Region']==region]
        if len(vrs)==0:
            raise Exception(f"Product {product} or Region {region} invalid. Cannot load CASC data")

        vr = vrs[0]
        bc = vr['BuildConfig']
        bc_f = getProductCDNFile(product,bc,region,ftype="config",enc="utf-8")
        self.build_config = parse_build_config(bc_f)

        cdn_f = parse_build_config(getProductCDNFile(product,vr['CDNConfig'],region,ftype="config",enc="utf-8"))
        archives = cdn_f['archives'].split()
        self.file_table={} # ckey -> fileinfo, populated over time instead of all at once, unlike DirCASCReader

        for a in archives:
            i = getProductCDNFile(product,a,region,ftype="data",index=True,cache_dur=-1)
            try:
                ed=r_cidx(i)
                for xi in ed:
                    if xi in self.file_table:
                        continue
                    x=ed[xi]
                    x.data_file=a
                    self.file_table[xi] = x
            except AssertionError as e:
                print("archive index file " + a + " did not match assertions, ignoring this for now since it only causes minor issues.")
                # raise e
        
        self.uid = self.build_config['build-uid']
        root_ckey = self.build_config['root']
        enc_hash1,enc_ekey = self.build_config['encoding'].split()
        inst_hash1,_ = self.build_config['install'].split()
        download_hash1,_ = self.build_config['download'].split()
        size_hash1,_ = self.build_config['size'].split()

        encfile = getProductCDNFile(product,enc_ekey,region,ftype="data",cache_dur=-1) # enc files never change. not that i know of
        encfile = parse_blte(encfile)[1]

        self.ckey_map = parse_encoding_file(encfile)
        print(f"[CTBL] {len(self.ckey_map)}")

        root_file = self.get_file_by_ckey(root_ckey)
        self.file_translate_table = parse_root_file(self.uid,root_file,self) # maps some ID(can be filedataid, path, whatever) -> ckey
        print(f"[FTTBL] {len(self.file_translate_table)}")

        self.file_translate_table.append((NAMED_FILE,"_ROOT",root_ckey))
        
        self.ckey_map[int(enc_hash1,16)] = int(enc_ekey,16) # map the encoding file's ckey to its own ekey on the ckey-ekey map, since it appears to not be included in the enc-table
        self.file_translate_table.append((NAMED_FILE,"_ENCODING",enc_hash1))
        self.file_translate_table.append((NAMED_FILE,"_INSTALL",inst_hash1))
        self.file_translate_table.append((NAMED_FILE,"_DOWNLOAD",download_hash1))
        self.file_translate_table.append((NAMED_FILE,"_SIZE",size_hash1))

        CASCReader.__init__(self)

    def list_files(self):
        files=[]
        for x in self.file_translate_table:
            if x[0] == NAMED_FILE:
                files.append((x[1],x[2]))
        return files

    def list_unnamed_files(self):
        return []

    def get_file_info_by_ckey(self, ckey):
        if isinstance(ckey,str):
            ckey=int(ckey,16)

        if ckey not in self.ckey_map:
            return None

        if self.ckey_map[ckey] in self.file_table:
            return self.file_table[ self.ckey_map[ckey]]
        else:
            if ckey not in self.ckey_map:
                return None

            fi = FileInfo()
            fi.ckey = ckey
            fi.ekey = self.ckey_map[ckey]
            self.file_table[fi.ekey]=fi
            return fi

    def _get_file_blte(self,finfo,with_data=True,max_size=-1):
        from requests.exceptions import HTTPError
        if hasattr(finfo,"data_file") and finfo.data_file is not None:
            archive_file = getProductCDNFile(self.product,finfo.data_file,max_size=max_size)
            return parse_blte(archive_file[finfo.offset:finfo.offset+finfo.compressed_size])
            # print(finfo.data_file,finfo.offset,finfo.compressed_size)
        else:
            ekey = f"{finfo.ekey:032x}"
            print(ekey,f"{finfo.ckey:032x}")
            return parse_blte(getProductCDNFile(self.product,ekey,max_size=max_size),read_data=with_data)

    def _populate_file_info_sizes(self,finfo):
        blte_header,_ = self._get_file_blte(finfo,with_data=False)
        finfo.chunk_count=len(blte_header[3])
        finfo.uncompressed_size=0
        for c in blte_header[3]: # for each chunk
            finfo.uncompressed_size+=c[1]

    def get_file_size_by_ckey(self, ckey):
        finfo = self.get_file_info_by_ckey(ckey)
        if finfo == None:
            return None
        if not hasattr(finfo,"uncompressed_size") or finfo.uncompressed_size is None:
            self._populate_file_info_sizes(finfo)
        return finfo.uncompressed_size
    
    def get_chunk_count_by_ckey(self, ckey):
        finfo = self.get_file_info_by_ckey(ckey)
        if finfo == None:
            return None
        if not hasattr(finfo,"uncompressed_size") or finfo.uncompressed_size is None:
            self._populate_file_info_sizes(finfo)
        return finfo.chunk_count

    def get_file_by_ckey(self,ckey,max_size=-1):
        finfo = self.get_file_info_by_ckey(ckey)
        if finfo == None:
            return None
        return self._get_file_blte(finfo,max_size=max_size)[1]

class DirCASCReader(CASCReader):
    def __init__(self,path):
        if not os.path.exists(path+"/.build.info") or not os.path.exists(path+"/Data/data"):
            raise Exception("Not a valid CASC datapath")
        self.path = path
        self.build_path = self.path+"/.build.info"
        self.data_path = self.path+"/Data/data/"

        build_file,self.build_config=None,None
        with open(self.build_path,"r") as b:
            build_file = parse_config(b.read())[0]
        with open(path+"/Data/config/"+prefix_hash(build_file['Build Key']),"r") as b:
            self.build_config = parse_build_config(b.read())
        print("[BF]")

        assert build_file is not None and self.build_config is not None

        self.uid = self.build_config['build-uid']
        root_ckey = self.build_config['root']
        enc_hash1,enc_hash2 = self.build_config['encoding'].split()
        inst_hash1,_ = self.build_config['install'].split()
        download_hash1,_ = self.build_config['download'].split()
        size_hash1,_ = self.build_config['size'].split()

        self.file_table = {} # maps ekey -> fileinfo (size, datafile, offset)
        files = os.listdir(self.data_path)
        for x in files:
            if x[-4:]==".idx":
                # i,v=x[:2],x[2:-4]
                ents=r_idx(self.data_path+x)
                for e in ents:
                    if e.ekey not in self.file_table: # since apparently duplicates exist and are wrong.... YAY!
                        self.file_table[e.ekey]=e

        print(f"[ETBL] {len(self.file_table)}")

        enc_info = self.file_table[int(enc_hash2[:18],16)]
        enc_file = r_cascfile(self.data_path,enc_info.data_file,enc_info.offset)
        
        # Load the CKEY MAP from the encoding file.
        self.ckey_map = parse_encoding_file(enc_file) # maps ckey(hexstr) -> ekey(int of first 9 bytes)
        print(f"[CTBL] {len(self.ckey_map)}")

        root_file = self.get_file_by_ckey(root_ckey)
        self.file_translate_table = parse_root_file(self.uid,root_file,self) # maps some ID(can be filedataid, path, whatever) -> ckey
        print(f"[FTTBL] {len(self.file_translate_table)}")

        self.file_translate_table.append((NAMED_FILE,"_ROOT",root_ckey))
        
        self.ckey_map[int(enc_hash1,16)] = int(enc_hash2[:18],16) # map the encoding file's ckey to its own ekey on the ckey-ekey map, since it appears to not be included in the enc-table
        self.file_translate_table.append((NAMED_FILE,"_ENCODING",enc_hash1))
        self.file_translate_table.append((NAMED_FILE,"_INSTALL",inst_hash1))
        self.file_translate_table.append((NAMED_FILE,"_DOWNLOAD",download_hash1))
        self.file_translate_table.append((NAMED_FILE,"_SIZE",size_hash1))

        CASCReader.__init__(self)
        
    def get_file_size_by_ckey(self,ckey):
        finfo = self.get_file_info_by_ckey(ckey)
        if finfo == None:
            return None
        if not hasattr(finfo,"uncompressed_size") or finfo.uncompressed_size is None:
            finfo.uncompressed_size, finfo.chunk_count = cascfile_size(self.data_path,finfo.data_file,finfo.offset)
        return finfo.uncompressed_size

    def get_chunk_count_by_ckey(self,ckey):
        finfo = self.get_file_info_by_ckey(ckey)
        if finfo == None:
            return None
        if not hasattr(finfo,"chunk_count") or finfo.chunk_count is None:
            finfo.uncompressed_size, finfo.chunk_count = cascfile_size(self.data_path,finfo.data_file,finfo.offset)
        return finfo.chunk_count

    def get_file_by_ckey(self,ckey,max_size=-1):
        finfo = self.get_file_info_by_ckey(ckey)
        if finfo == None:
            return None
        return r_cascfile(self.data_path,finfo.data_file,finfo.offset,max_size)

    def list_files(self):
        """Returns a list of tuples, each tuple of format (FileName, CKey)"""
        files = []
        for x in self.ckey_map:
            first_ekey = self.ckey_map[x]
            if first_ekey in self.file_table: # check if the ckey_map entry is inside the file.
                n=self.get_name(x)
                if n is not None:
                    files.append((n,x))
        return files
    
    def list_unnamed_files(self):
        """Returns a list of tuples, each tuple of format (Ckey,Ckey) (to match with named files list)"""
        files = []
        for ckey in self.ckey_map:
            first_ekey = self.ckey_map[ckey]
            if first_ekey in self.file_table:
                n=self.get_name(ckey)
                if n is None:
                    files.append((ckey,ckey))
        return files
    
    def get_file_info_by_ckey(self, ckey):
        """Takes ckey in either int form or hex form"""
        if isinstance(ckey,str):
            ckey=int(ckey,16)

        try:
            return self.file_table[self.ckey_map[ckey]]
        except:
            return None

if __name__ == '__main__':
    import cProfile, io
    from pstats import SortKey,Stats
    pr = cProfile.Profile()
    pr.enable()

    # On my pc, these are some paths:
    # cr = DirCASCReader("G:/Misc Games/Warcraft III") # War 3
    # cr = DirCASCReader("G:/Misc Games/Diablo III") # Diablo 3
    # On my mac, these are the paths:
    # cr = DirCASCReader("/Users/sepehr/Diablo III") #Diablo 3
    # cr = DirCASCReader("/Applications/Warcraft III") # War 3
    cr = CDNCASCReader("w3")
    print(f"{len(cr.list_files())} named files loaded in list")

    pr.disable()
    s = io.StringIO()
    sortby = SortKey.TIME
    ps = Stats(pr, stream=s).sort_stats(sortby)
    ps.print_stats()
    print('\n'.join(s.getvalue().split("\n")[:20]))
    import time
    time.sleep(15)