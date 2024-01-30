import torch
from torch.utils.data import Dataset, DataLoader, TensorDataset
import torch.optim as optim
from tqdm.notebook import tqdm

def compute_xc_yc(tip):
    """
    Compute the center position of the tip
        Input: tip (tensor of size (tip_height, tip_width))
        Output: xc, yc (int)
    """
    tip_xsiz, tip_ysiz = tip.size()
    xc = round((tip_xsiz - 1) / 2)
    yc = round((tip_ysiz - 1) / 2)
    return xc, yc

def idilation(surface, tip):
    """
    Compute the dilation of surface by tip
        Input: surface (tensor of size (surface_height, surface_width)
               tip (tensor of size (tip_height, tip_width)
        Output: r (tensor of size (image_height, image_width)
                where image_heigh is equal to surface_height
                      image_width is equal to surface_width
    """
    xc, yc = compute_xc_yc(tip)
    tip_xsiz, tip_ysiz = tip.size()

    r = torch.full_like(surface, -float('inf'))
    for px in range(-xc, tip_xsiz - xc):
        for py in range(-yc, tip_ysiz - yc):
            temp = torch.roll(surface, shifts=(-px, -py), dims=(0, 1))
            temp = temp + tip[xc + px, yc + py]
            r = torch.maximum(r, temp)
    return r

def ierosion(image, tip):
    """
    Compute the erosion of image by tip
        Input: image (tensor of size (image_height, image_width)
               tip (tensor of size (tip_height, tip_width)
        Output: r (tensor of size (image_height, image_width)
    """
    xc, yc = compute_xc_yc(tip)
    tip_xsiz, tip_ysiz = tip.size()

    r = torch.full_like(image, float('inf'))
    for px in range(-xc, tip_xsiz - xc):
        for py in range(-yc, tip_ysiz - yc):
            temp = torch.roll(image, shifts=(px, py), dims=(0, 1))
            temp = temp - tip[xc + px, yc + py]
            r = torch.minimum(r, temp)
    return r

def translate_tip_mean(P, cutoff=10**(-8)):
    """
    Translate the tip to the center of mass
        Input: P (tensor of size (tip_height, tip_width))
        Output: P_new (tensor of size (tip_height, tip_width)
    """
    tip_xsiz, tip_ysiz = P.size()
    xc, yc = compute_xc_yc(P)

    #p_max = torch.min(P)
    #P = P - p_max

    p_min = torch.min(P)
    weight = P - p_min
    weight = weight

    id = weight < cutoff
    weight[id] = 0.0

    if torch.all(weight < 10**(-10)):
        weight.fill_(1.0)

    ix = torch.ones(tip_xsiz, tip_ysiz, dtype=P.dtype, device=P.device)
    iy = torch.ones(tip_xsiz, tip_ysiz, dtype=P.dtype, device=P.device)
    ix = torch.cumsum(ix, dim=0) - 1.0
    iy = torch.cumsum(iy, dim=1) - 1.0
    com_x = torch.sum(ix * weight / torch.sum(weight))
    com_y = torch.sum(iy * weight / torch.sum(weight))
    id_x = round(com_x.item())
    id_y = round(com_y.item())

    pxmin = max(- xc, - id_x)
    pymin = max(- yc, - id_y)
    pxmax = min(tip_xsiz - xc, tip_xsiz - id_x)
    pymax = min(tip_ysiz - yc, tip_ysiz - id_y)

    P_new = torch.full_like(P, p_min.item())
    P_new[(xc + pxmin):(xc + pxmax), (yc + pymin):(yc + pymax)] = P[(id_x + pxmin):(id_x + pxmax), (id_y + pymin):(id_y + pymax)]

    return P_new

def differentiable_btr(images, tip_size, nepoch=100, lr=0.1, weight_decay=0.0):
    """
    Reconstruct tip shape from given AFM images by differentiable blind tip reconstruction (BTR)
        Input: images (tensor of size (nframe, image_height, image_width)
               tip_size (2d tuple)
               nepoch (int)
               lr (float) for AdamW
               weight_decay (float) for AdamW
        Output: tip_estimate (tensor of tip_size), loss_train (list)
    """
    # Initialize tip with zeros
    device = images.device
    tip = torch.zeros(tip_size, dtype=torch.float64, requires_grad=True, device=device)

    # Optimization settings
    optimizer = optim.AdamW([tip], lr=lr, weight_decay=weight_decay)

    loss_train = []
    for epoch in tqdm(range(nepoch)):
        loss_tmp = 0.0
        for iframe in range(images.shape[0]):
            optimizer.zero_grad()
            image_reconstructed = idilation(ierosion(images[iframe, :, :], tip), tip)
            loss = torch.mean((image_reconstructed - images[iframe, :, :])**2)
            loss.backward()
            optimizer.step()
            with torch.no_grad():
                tip.data = torch.clamp(tip, max=0.0)
                tip.data = translate_tip_mean(tip)
            image_reconstructed = idilation(ierosion(images[iframe, :, :], tip), tip)
            loss = torch.mean((image_reconstructed - images[iframe, :, :])**2)
            loss_tmp += loss.item()
        #if epoch % 1 == 0:
        #    print(f"Epoch: {epoch}, Loss: {loss_tmp}")
        loss_train.append(loss_tmp)

    tip_estimate = tip.detach()
    return tip_estimate, loss_train

def surfing(xyz, radius, config):
    """
    Compute the maximum height (z-value) of molecular surface at grid points on AFM stage (where z=0)
        Input: xyz (tensor of size (*, N, 3))
                radius (tensor of size (N,))
                config (dict)
        Output: z_stage (tensor of size (*, len(y_stage), len(x_stage))
    """
    radius2 = radius**2
    x_stage = torch.arange(config["min_x"], config["max_x"], config["resolution_x"], dtype=xyz.dtype, device=xyz.device) + 0.5*config["resolution_x"] #(W,)
    y_stage = torch.arange(config["min_y"], config["max_y"], config["resolution_y"], dtype=xyz.dtype, device=xyz.device) + 0.5*config["resolution_y"] #(H,)

    dx = xyz[...,0,None] - x_stage #(*,N,W)
    dx2 = dx**2 #(*,N,W)
    dy = xyz[...,1,None] - y_stage #(*,N,H)
    dy2 = dy**2 #(*,N,H)
    r2 = dx2.unsqueeze(-2) + dy2[...,None] #(*,N,H,W)
    index_within_radius = r2 < radius2[...,None,None] #(*,N,H,W)
    temp = xyz[...,2,None,None] + torch.sqrt(radius2[...,None,None] - r2) #(*,N,H,W)
    temp[~index_within_radius] = -torch.inf
    temp_max = temp.max(dim=-3)[0] #(*,H,W)
    z_stage = torch.where(index_within_radius.any(dim=-3), temp_max, torch.zeros_like(temp_max, dtype=xyz.dtype, device=xyz.device)) #(H,W)
    return z_stage.flip([-2])

def surfing_old(xyz, radius, config):
    """
    Compute the maximum height (z-value) of molecular surface at grid points on AFM stage (where z=0)
        Input: xyz (tensor of size (N, 3))
                radius (tensor of size (N,))
                config (dict)
        Output: z_stage (tensor of size (len(y_stage), len(x_stage))
    """
    device = xyz.device
    radius2 = radius**2
    x_stage = torch.arange(config["min_x"], config["max_x"], config["resolution_x"]) + 0.5*config["resolution_x"]
    y_stage = torch.arange(config["min_y"], config["max_y"], config["resolution_y"]) + 0.5*config["resolution_y"]
    #z_stage = torch.full((len(y_stage), len(x_stage)), xyz[:, 2].min())
    z_stage = torch.full((len(y_stage), len(x_stage)), 0.0, dtype=torch.float32, device=device)
    for i in range(len(x_stage)):
        x = x_stage[i]
        dx = xyz[:, 0] - x
        dx2 = dx**2
        for j in range(len(y_stage)):
            y = y_stage[j]
            dy = xyz[:, 1] - y
            dy2 = dy**2
            r2 = dx2 + dy2
            index_within_radius = r2 < radius2
            #print(r2[:3], radius[:3])
            if any(index_within_radius):
                z_stage[-j-1, i] = torch.max(xyz[index_within_radius, 2] + torch.sqrt(radius2[index_within_radius] - r2[index_within_radius]))
    return z_stage

def afmize(xyz, tip, radius, config):
    """
    Compute AFM image from xyz coordinates and atomic radii
        Input: xyz (tensor of size (N, 3))
                tip (tensor of size (tip_height, tip_width))
                radius (tensor of size (N,))
                config (dict)
        Output: image (tensor of size (len(y_stage), len(x_stage))
    """
    surface = surfing(xyz, radius, config)
    image = idilation(surface, tip)
    return image

# mapping atom name to radius in Angstrom
Atom2Radius = {
    "H": 0.120,
    "HE": 0.140,
    "B": 0.192,
    "C": 0.170,
    "CA": 0.170,
    "CB": 0.170,
    "CG": 0.170,
    "CG1": 0.170,
    "CG2": 0.170,
    "CG3": 0.170,
    "CD": 0.170,
    "CD1": 1.70,
    "CD2": 0.170,
    "CD3": 0.170,
    "CZ": 0.170,
    "CZ1": 0.170,
    "CZ2": 0.170,
    "CZ3": 0.170,
    "CE": 0.170,
    "CE1": 0.170,
    "CE2": 0.170,
    "CE3": 0.170,
    "CH": 0.170,
    "CH1": 0.170,
    "CH2": 0.170,
    "CH3": 0.170,
    "N": 0.155,
    "NE": 0.155,
    "NZ": 0.155,
    "ND1": 0.155,
    "ND2": 0.155,
    "NE1": 0.155,
    "NE2": 0.155,
    "NH1": 0.155,
    "NH2": 0.155,
    "O": 0.152,
    "OH": 0.152,
    "OG": 0.152,
    "OE1": 0.152,
    "OE2": 0.152,
    "OG1": 0.152,
    "OG2": 0.152,
    "OD1": 0.152,
    "OD2": 0.152,
    "OXT": 0.152,
    "F": 0.147,
    "MG": 0.173,
    "AL": 0.184,
    "SI": 0.210,
    "P": 0.180,
    "S": 0.180,
    "SD": 0.180,
    "SG": 0.180,
    "CL": 0.175,
    "AR": 0.188,
    "K": 0.275,
    "CYS": 0.275,
    "PHE": 0.32,
    "LEU": 0.31,
    "TRP": 0.34,
    "VAL": 0.295,
    "ILE": 0.31,
    "MET": 0.31,
    "HIS": 0.305,
    "HSD": 0.305,
    "TYR": 0.325,
    "ALA": 0.25,
    "GLY": 0.225,
    "PRO": 0.28,
    "ASN": 0.285,
    "THR": 0.28,
    "SER": 0.26,
    "ARG": 0.33,
    "GLN": 0.30,
    "ASP": 0.28,
    "LYS": 0.32,
    "GLU": 0.295
}
