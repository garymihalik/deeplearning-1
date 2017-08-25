from imports import *
from torch_imports import *
from fast_gen import *
from layer_optimizer import *

imagenet_stats = ([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
inception_stats = ([0.5, 0.5, 0.5], [0.5, 0.5, 0.5])
inception_models = (inception_4, inceptionresnet_2)

def get_cv_idxs(n, cv_idx=4, val_pct=0.2, seed=42):
    np.random.seed(seed)
    n_val = int(val_pct*n)
    idx_start = cv_idx*n_val
    idxs = np.random.permutation(n)
    return idxs[idx_start:idx_start+n_val]

def resize_img(fname, targ, path, new_path):
    dest = os.path.join(path,new_path,str(targ),fname)
    if os.path.exists(dest): return
    im = PIL.Image.open(os.path.join(path, fname)).convert('RGB')
    r,c = im.size
    ratio = targ/min(r,c)
    sz = (scale_to(r, ratio, targ), scale_to(c, ratio, targ))
    os.makedirs(os.path.split(dest)[0], exist_ok=True)
    im.resize(sz, PIL.Image.LINEAR).save(dest)

def resize_imgs(fnames, targ, path, new_path):
    with ThreadPoolExecutor(8) as e:
        ims = e.map(lambda x: resize_img(x, targ, path, 'tmp'), fnames)
        for x in tqdm(ims, total=len(fnames), leave=False): pass
    return os.path.join(path,new_path,str(targ))
        
def read_dir(path, folder):
    full_path = os.path.join(path, folder)
    fnames = iglob(f"{full_path}/*.*")
    return [os.path.relpath(f,path) for f in fnames]
        
def read_dirs(path, folder):
    full_path = os.path.join(path, folder)
    all_labels = sorted([os.path.basename(os.path.dirname(f)) 
                  for f in iglob(f"{full_path}/*/")])
    fnames = [iglob(f"{full_path}/{d}/*.*") for d in all_labels]
    pairs = [(os.path.relpath(fn,path), l) for l,f in zip(all_labels, fnames) for fn in f]
    return list(zip(*pairs))+[all_labels]

def n_hot(ids, c):
    res = np.zeros((c,), dtype=np.float32)
    res[ids] = 1
    return res

def folder_source(path, folder):
    fnames, lbls, all_labels = read_dirs(path, folder)
    label2idx = {v:k for k,v in enumerate(all_labels)}
    idxs = [label2idx[lbl] for lbl in lbls]
    c = len(all_labels)
    label_arr = np.array(idxs, dtype=int)
    return fnames, label_arr, all_labels

def parse_csv_labels(fn, skip_header=True):
    skip = 1 if skip_header else 0
    csv_lines = [o.strip().split(',') for o in open(fn)][skip:]
    csv_labels = {a:b.split(' ') for a,b in csv_lines}
    all_labels = sorted(list(set(p for o in csv_labels.values() for p in o)))
    label2idx = {v:k for k,v in enumerate(all_labels)}
    return sorted(csv_labels.keys()), csv_labels, all_labels, label2idx

def nhot_labels(label2idx, csv_labels, fnames, c):
    all_idx = {k: n_hot([label2idx[o] for o in v], c) 
               for k,v in csv_labels.items()}
    return np.stack([all_idx[o] for o in fnames])

def csv_source(folder, csv_file, skip_header=True, suffix=''):
    fnames,csv_labels,all_labels,label2idx = parse_csv_labels(
        csv_file, skip_header)
    label_arr = nhot_labels(label2idx, csv_labels, fnames, len(all_labels))
    full_names = [os.path.join(folder,fn+suffix) for fn in fnames]
    is_single = np.all(label_arr.sum(axis=1)==1)
    if is_single: label_arr = np.argmax(label_arr, axis=1)
    return full_names, label_arr, all_labels

class BaseDataset(Dataset):
    def __init__(self, transform, target_transform):
        self.transform,self.target_transform = transform,target_transform
        self.lock=threading.Lock()
        self.n = self.get_n()
        self.c = self.get_c()
        self.sz = self.get_sz()
 
    def __getitem__(self, idx):
        return (self.get(self.transform, self.get_x, idx), 
                self.get(self.target_transform, self.get_y, idx))

    def __len__(self): return self.n
        
    def get(self, tfm, fn, idx): 
        return fn(idx) if tfm is None else tfm(fn(idx))
        
    @abstractmethod
    def get_n(self): raise NotImplementedError
    @abstractmethod
    def get_c(self): raise NotImplementedError
    @abstractmethod
    def get_sz(self): raise NotImplementedError
    @abstractmethod
    def get_x(self, i): raise NotImplementedError
    @abstractmethod
    def get_y(self, i): raise NotImplementedError
    @property
    def is_multi(self): return False

    
class FilesDataset(BaseDataset):
    def __init__(self, fnames, transform, path):
        self.path,self.fnames = path,fnames
        super().__init__(transform, None)
    def get_n(self): return len(self.y)
    def get_sz(self): return self.transform.sz
    def get_x(self, i): 
        im = PIL.Image.open(os.path.join(self.path, self.fnames[i])).convert('RGB')
        return np.array(im, dtype=np.float32)/255.
    def resize_imgs(self, targ, new_path):
        dest = resize_imgs(self.fnames, targ, self.path, new_path)
        return self.__class__(self.fnames, self.y, self.transform, dest)

            
class FilesArrayDataset(FilesDataset):
    def __init__(self, fnames, y, transform, path):
        self.y=y
        assert(len(fnames)==len(y))
        super().__init__(fnames, transform, path)
    def get_y(self, i): return self.y[i]

    
class FilesIndexArrayDataset(FilesArrayDataset):
    def get_c(self): return int(self.y.max())+1

    
class FilesNhotArrayDataset(FilesArrayDataset):
    def get_c(self): return self.y.shape[1]
    @property
    def is_multi(self): return True

    
class ArraysDataset(BaseDataset):
    def __init__(self, x, y, transform):
        self.x,self.y=x,y
        assert(len(x)==len(y))
        super().__init__(transform, None)
    def get_x(self, i): 
        with self.lock: return self.x[i]
    def get_y(self, i): 
        with self.lock: return self.y[i]
    def get_n(self): return len(self.y)
    def get_sz(self): return self.x.shape[1]

    
class ArraysIndexDataset(ArraysDataset):
    def get_c(self): return int(self.y.max())+1

    
class ArraysNhotDataset(ArraysDataset):
    def get_c(self): return self.y.shape[1]
    @property
    def is_multi(self): return True

    
class ModelData():
    def __init__(self, trn_dl, val_dl): self.trn_dl,self.val_dl = trn_dl,val_dl
        
        
class ImageData(ModelData):
    def __init__(self, path, datasets, bs, num_workers, classes): 
        trn_ds,val_ds,fix_ds,aug_ds,test_ds,test_aug_ds = datasets
        self.path,self.bs,self.num_workers,self.classes = path,bs,num_workers,classes
        self.trn_dl,self.val_dl,self.fix_dl,self.aug_dl,self.test_dl,self.test_aug_dl = [
            self.get_dl(ds,shuf) for ds,shuf in [
                (trn_ds,True),(val_ds,False),(fix_ds,False),(aug_ds,False),
                (test_ds,False),(test_aug_ds,False)
            ]
        ]

    def get_dl(self, ds, shuffle):
        if ds is None: return None
        return DataLoader(ds, batch_size=self.bs, shuffle=shuffle,
            num_workers=self.num_workers, pin_memory=True)

    @property
    def trn_ds(self): return self.trn_dl.dataset
    @property
    def val_ds(self): return self.val_dl.dataset
    @property
    def sz(self): return self.trn_ds.sz
    @property
    def c(self): return self.trn_ds.c
    @property
    def trn_y(self): return self.trn_ds.y
    @property
    def val_y(self): return self.val_ds.y

    def resized(self, dl, targ, new_path):
        return dl.dataset.resize_imgs(targ,new_path) if dl else None
        
    def resize(self, targ, new_path):
        new_ds = []
        dls = [self.trn_dl,self.val_dl,self.fix_dl,self.aug_dl]
        if self.test_dl: dls += [self.test_dl, self.test_aug_dl]
        else: dls += [None,None]
        t = tqdm(dls)
        for dl in t: new_ds.append(self.resized(dl, targ, new_path))
        t.close()
        return self.__class__(new_ds[0].path, new_ds, self.bs, self.num_workers, self.classes)

    
class ImageClassifierData(ImageData):
    @property
    def is_multi(self): return self.trn_dl.dataset.is_multi
        
    @classmethod
    def get_ds(self, fn, trn, val, tfms, test=None, **kwargs):
        res = [
            fn(trn[0], trn[1], tfms[0], **kwargs), # train
            fn(val[0], val[1], tfms[1], **kwargs), # val
            fn(trn[0], trn[1], tfms[1], **kwargs), # fix
            fn(val[0], val[1], tfms[0], **kwargs)  # aug
        ]
        if test:
            test_lbls = np.zeros((len(test),1))
            res += [
                fn(test, test_lbls, tfms[1], **kwargs), # test
                fn(test, test_lbls, tfms[0], **kwargs)  # test_aug
            ]
        else: res += [None,None]
        return res
        
    @classmethod
    def from_arrays(self, path, trn, val, bs, tfms=(None,None), classes=None, num_workers=4):
        datasets = self.get_ds(ArraysIndexDataset, trn, val, tfms)
        return self(path, datasets, bs, num_workers, classes=classes)

    @classmethod
    def from_paths(self, path, bs, tfms, trn_name='train', val_name='val', test_name=None, num_workers=4):
        trn,val = [folder_source(path, o) for o in ('train', 'valid')]
        test_fnames = read_dir(path, test_name) if test_name else None
        datasets = self.get_ds(FilesIndexArrayDataset, trn, val, tfms, path=path, test=test_fnames)
        return self(path, datasets, bs, num_workers, classes=trn[2])

    @classmethod
    def from_csv(self, path, folder, csv_fname, bs, tfms,
               val_idxs=None, suffix='', test_name=None, skip_header=True, num_workers=4): 
        fnames,y,classes = csv_source(folder, csv_fname, skip_header, suffix)
        ((val_fnames,trn_fnames),(val_y,trn_y)) = split_by_idx(val_idxs, fnames, y)
        
        test_fnames = read_dir(path, test_name) if test_name else None
        f = FilesIndexArrayDataset if len(trn_y.shape)==1 else FilesNhotArrayDataset
        datasets = self.get_ds(f, (trn_fnames,trn_y), (val_fnames,val_y), tfms,
                               path=path, test=test_fnames)
        return self(path, datasets, bs, num_workers, classes=classes)

def split_by_idx(idxs, *a):
    a = [np.array(o) for o in a]
    mask = np.zeros(len(a[0]),dtype=bool)
    mask[np.array(idxs)] = True
    return [(o[mask],o[~mask]) for o in a]

def tfms_from_model(f_model, sz, aug_tfms=[], max_zoom=None, pad=0):
    stats = inception_stats if f_model in inception_models else imagenet_stats
    tfm_norm = Normalize(*stats)
    val_tfm = image_gen(tfm_norm, sz, pad=pad)
    trn_tfm=image_gen(tfm_norm, sz, tfms=aug_tfms, max_zoom=max_zoom, pad=pad)
    return trn_tfm, val_tfm
