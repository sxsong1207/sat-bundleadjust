import numpy as np
import pickle
import os
import time
import json
import matplotlib.pyplot as plt
from scipy.optimize import least_squares, minimize
from PIL import Image

from bundle_adjust import ba_utils
from bundle_adjust import ba_core
from bundle_adjust import rpc_fit

class BundleAdjustmentPipeline:
    def __init__(self, ba_input_data, feature_detection=True, tracks_config=None, satellite=True, display_plots=False):
        
        
        self.satellite = satellite
        self.feature_detection = feature_detection
        self.tracks_config = tracks_config
        self.display_plots = display_plots
        self.input_dir = ba_input_data['input_dir']
        self.output_dir = ba_input_data['output_dir']
        self.n_adj = ba_input_data['n_adj']
        self.n_new = ba_input_data['n_new']
        self.myimages = ba_input_data['image_fnames'].copy()
        self.crop_offsets = [{'col0':f['col0'], 'row0':f['row0']} for f in ba_input_data['crops']] 
        self.input_seq = [f['crop'] for f in ba_input_data['crops']]
        self.input_masks = ba_input_data['masks'].copy() if ba_input_data['masks'] is not None else None
        self.input_rpcs = ba_input_data['rpcs'].copy()
        self.cam_model = ba_input_data['cam_model']
        self.aoi = ba_input_data['aoi'] if ba_input_data['aoi'] is not None else self.define_aoi_from_input_crops()
        
        self.footprints = ba_utils.get_image_footprints(self.input_rpcs, ba_input_data['crops'])
        self.input_P = self.approximate_rpcs_as_proj_matrices()
        
        # stuff to be filled by 'run_feature_detection'
        self.features = []
        self.pairs_to_triangulate = []
        self.pairs_to_match = []
        self.C = None
        
        # stuff to be filled by 'define_ba_parameters'
        self.params_opt = None
        self.cam_params = None 
        self.pts_3d = None 
        self.pts_2d = None 
        self.cam_ind = None 
        self.pts_ind = None 
        self.ba_params = None
        
        # stuff to be filled by 'run_ba_optimization'
        self.pts_3d_ba = None
        self.cam_params_ba = None
        self.P_crop_ba = None
        self.ba_e = None
        
        
        
    def display_aoi(self):
        ba_utils.display_rois_over_map([self.aoi], zoom_factor = 14)
    
    def define_aoi_from_input_crops(self):
        aois = []
        for crop_offset, rpc, img in zip(self.crop_offsets, self.input_rpcs, self.input_seq):
            # if (y,x) = (0,0) then this function should be equivalent to IS18.utils.get_image_longlat_polygon(fname)
            y,x = crop_offset['row0'], crop_offset['col0']
            h,w = img.shape
            crop_coords = np.array([[y, x], [y, x+w], [y+h, x+w], [y+h, x]])
            row, col = crop_coords[:,0].tolist(), crop_coords[:,1].tolist()
            lon, lat = rpc.localization(col, row, [0.0]*len(row), return_normalized=False)
            lonlat_coords = np.array([np.vstack((lon, lat)).T]).tolist()
            current_aoi = {'coordinates': lonlat_coords, 'type': 'Polygon'}
            current_aoi['center'] = np.mean(current_aoi['coordinates'][0][:4], axis=0).tolist()
            aois.append(current_aoi)
        return ba_utils.combine_aoi_borders(aois)
        
    
    def compute_feature_tracks(self):
        
        if self.satellite:
            from feature_tracks.ft_pipeline import FeatureTracksPipeline
            
            local_data = {'n_adj': self.n_adj, 'n_new': self.n_new, 'fnames': self.myimages, 'images': self.input_seq,
                          'rpcs': self.input_rpcs, 'offsets': self.crop_offsets,  'footprints': self.footprints,
                          'proj_matrices': self.input_P, 'cam_model': self.cam_model, 'masks': self.input_masks}
        
            if not self.feature_detection:
                local_data['n_adj'] = self.n_adj + self.n_new
                local_data['n_new'] = 0
        
        ft_pipeline = FeatureTracksPipeline(self.input_dir, self.output_dir, local_data,
                                            config=self.tracks_config, satellite=self.satellite)
        
        feature_tracks = ft_pipeline.build_feature_tracks()
            
        self.features = feature_tracks['features']
        self.pairwise_matches = feature_tracks['pairwise_matches']
        self.pairs_to_triangulate = feature_tracks['pairs_to_triangulate']
        self.pairs_to_match = feature_tracks['pairs_to_match']
        self.C = feature_tracks['C']
        self.C_v2 = feature_tracks['C_v2']
        
        del feature_tracks
        
    
    def define_ba_parameters(self, verbose=False):

        '''
        INPUT PARAMETERS FOR BUNDLE ADJUSTMENT
        'cam_params': (n_cam, 12), initial projection matrices. 1 row = 1 camera estimate.
                      first 3 elements of each row = R vector, next 3 = T vector, then f and two dist. coef.
        'pts_3d'    : (n_pts, 3) contains the initial estimates of the 3D points in the world frame.
        'cam_ind'   : (n_observations,), indices of cameras (from 0 to n_cam - 1) involved in each observation.
        'pts_ind'   : (n_observations,) indices of points (from 0 to n_points - 1) involved in each observation.
        'pts_2d'    : (n_observations, 2) 2-D coordinates of points projected on images in each observations.
        '''

        print('Defining BA input parameters...')
        self.params_opt, self.cam_params, self.pts_3d, self.pts_2d, self.cam_ind, self.pts_ind, self.ba_params \
        = ba_core.set_ba_params(self.input_P, self.C, self.cam_model, \
                                self.n_adj, self.n_new, self.pairs_to_triangulate)
        print('...done!\n')

        if verbose:
            print('pts_2d.shape:{}  pts_ind.shape:{}  cam_ind.shape:{}'.format(self.pts_2d.shape, \
                                                                               self.pts_ind.shape, \
                                                                               self.cam_ind.shape))
            print('pts_3d.shape:{}  cam_params.shape:{}\n'.format(self.pts_3d.shape, \
                                                                  self.cam_params.shape))
            print('Bundle Adjustment parameters defined')

            if self.ba_params['n_params'] > 0 and self.ba_params['opt_X']:
                print('  -> Both camera parameters and 3D points will be optimized')
            elif self.ba_params['n_params'] > 0 and not self.ba_params['opt_X']:
                print('  -> Only the camera parameters will be optimized')
            else:
                print('  -> Only 3D points will be optimized')

        
    def run_ba_optimization(self, input_loss='linear', input_f_scale=1.0, input_ftol=1e-8, input_xtol=1e-8):
        
        # assign a weight to each observation
        pts_2d_w = np.ones(self.pts_2d.shape[0])
        
        if self.display_plots:
            plt.figure()
        
        # define input arguments
        input_args = (self.cam_ind, self.pts_ind, self.pts_2d, self.cam_params, self.pts_3d, self.ba_params, pts_2d_w)

        # compute loss value and plot residuals at the initial parameters
        f0 = ba_core.fun(self.params_opt, *input_args)
        if self.display_plots:
            plt.plot(f0)

        # define jacobian
        A = ba_core.bundle_adjustment_sparsity(self.cam_ind, self.pts_ind, self.ba_params)

        # run bundle adjustment
        t0 = time.time()
        res = least_squares(ba_core.fun, self.params_opt, jac_sparsity=A, verbose=1, x_scale='jac', method='trf', \
                            ftol=input_ftol, xtol=input_xtol, loss=input_loss, f_scale=input_f_scale, args=input_args)

        t1 = time.time()
        print("Optimization took {0:.0f} seconds\n".format(t1 - t0))

        #plot residuals at the found solution
        if self.display_plots:
            plt.plot(res.fun);

        # recover BA output
        self.pts_3d_ba, self.cam_params_ba, self.P_crop_ba \
        = ba_core.get_ba_output(res.x, self.ba_params, self.cam_params, self.pts_3d)
        
        # check BA error performance
        self.ba_e, self.init_e = ba_core.check_ba_error(f0, res.fun, pts_2d_w, display_plots=self.display_plots)
        
    def run_ba_softL1(self, f_scale=0.5, ftol=1e-4, xtol=1e-10):
        self.run_ba_optimization(input_loss='soft_l1', input_f_scale=f_scale, input_ftol=ftol, input_xtol=xtol)
        
    def run_ba_L2(self, ftol=1e-4, xtol=1e-10):
        self.run_ba_optimization(input_ftol=ftol, input_xtol=xtol)
    
    def clean_outlier_obs(self):

        elbow_value = ba_core.get_elbow_value(self.ba_e, verbose=self.display_plots)
        self.C = ba_core.remove_outlier_obs(self.ba_e, self.pts_ind, self.cam_ind, self.C, \
                                            self.pairs_to_triangulate, thr=max(elbow_value,2.0))
        self.define_ba_parameters()

        
    def save_corrected_matrices(self):
        
        os.makedirs(self.output_dir+'/P_adj', exist_ok=True)

        for im_idx in np.arange(self.n_adj, self.n_adj + self.n_new).astype(np.uint8):
            P_calib_fn = os.path.basename(os.path.splitext(self.myimages[im_idx])[0])+'_pinhole_adj.json'
            to_write = {
                # 'P_camera'
                # 'P_extrinsic'
                # 'P_intrinsic'
                "P": [self.P_crop_ba[im_idx][0,:].tolist(), 
                      self.P_crop_ba[im_idx][1,:].tolist(),
                      self.P_crop_ba[im_idx][2,:].tolist()],
                # 'exterior_orientation'
                "height": self.input_seq[im_idx].shape[0],
                "width": self.input_seq[im_idx].shape[1]        
            }

            with open(self.output_dir+'/P_adj/'+P_calib_fn, 'w') as json_file:
                json.dump(to_write, json_file, indent=4)
                
        print('\nBundle adjusted projection matrices successfully saved!\n')


    def save_corrected_rpcs(self, check_rpc_fitting_error=False, verbose=False): 
        
        #fit rpc

        import copy
        os.makedirs(self.output_dir+'/RPC_adj', exist_ok=True)
        
        # rpc fitting starts here
        myrpcs_calib = []
        if self.n_adj > 0:
            for im_idx in np.arange(self.n_adj):
                im_idx = int(im_idx)
                myrpcs_calib.append(copy.copy(self.input_rpcs[im_idx]))
        
        for im_idx in np.arange(self.n_adj + self.n_new): #np.arange(self.n_adj, self.n_adj + self.n_new):
            im_idx = int(im_idx)
            
            # calibrate and get error
            rpc_init = copy.copy(self.input_rpcs[im_idx])                  
            current_P = self.P_crop_ba[im_idx].copy()
            current_im = self.input_seq[im_idx].copy()
            current_ecef = self.pts_3d_ba.copy()
            rpc_calib, err_calib = rpc_fit.fit_rpc_from_projection_matrix(rpc_init, current_P, current_im, current_ecef)
            print('image {}, RMSE calibrated RPC = {}'.format(im_idx, err_calib))

            rpc_calib_fn = os.path.basename(os.path.splitext(self.myimages[im_idx])[0])+'_RPC_adj.txt'
            rpc_calib.write_to_file(self.output_dir+'/RPC_adj/'+rpc_calib_fn)
            myrpcs_calib.append(rpc_calib)

            # check the histogram of errors if the RMSE error is above subpixel
            if err_calib > 1.0 and verbose:
                col_pred, row_pred = rpc_calib.projection(lon, lat, alt)
                err = np.sum(abs(np.hstack([col_pred.reshape(-1, 1), row_pred.reshape(-1, 1)]) - target), axis=1)
                plt.figure()
                plt.hist(err, bins=30);
                plt.show()
           
        if verbose:
            for im_idx in range(int(self.C.shape[0]/2)):
                for p_idx in range(self.pts_3d_ba.shape[0]):
                        p_2d_gt = self.C[(im_idx*2):(im_idx*2+2),p_idx]
                        current_p = self.pts_3d_ba[p_idx,:]
                        lat, lon, alt = ba_utils.ecef_to_latlon_custom(current_p[0], current_p[1], current_p[2])
                        proj = self.input_P[im_idx] @ np.expand_dims(np.hstack((current_p, np.ones(1))), axis=1)
                        p_2d_proj = (proj[0:2,:] / proj[-1,-1]).ravel()
                        col, row = self.input_rpcs[im_idx].projection(lon, lat, alt)
                        p_2d_proj_rpc = np.hstack([col - self.crop_offsets[im_idx]['x0'], \
                                                   row - self.crop_offsets[im_idx]['y0']]).ravel()
                        proj = self.P_crop_ba[im_idx] @ np.expand_dims(np.hstack((current_p, np.ones(1))), axis=1)
                        p_2d_proj_ba = (proj[0:2,:] / proj[-1,-1]).ravel()
                        col, row = myrpcs_calib[im_idx].projection(lon, lat, alt)
                        p_2d_proj_rpc_ba = np.hstack([col, row])

                        reprojection_error_P = np.sum(abs(p_2d_proj_ba - p_2d_gt))
                        reprojection_error_RPC = np.sum(abs(p_2d_proj_rpc_ba - p_2d_gt))

                        if abs(reprojection_error_RPC - reprojection_error_P) > 0.001:
                            print('GT location   : {:.4f} , {:.4f}'.format(p_2d_gt[0], p_2d_gt[1])) 
                            print('RPC proj      : {:.4f} , {:.4f}'.format(p_2d_proj_rpc[0], p_2d_proj_rpc[1]))
                            print('P proj        : {:.4f} , {:.4f}'.format(p_2d_proj[0], p_2d_proj[1]))
                            print('P proj   (BA) : {:.4f} , {:.4f}'.format(p_2d_proj_ba[0], p_2d_proj_ba[1]))
                            print('RPC proj (BA) : {:.4f} , {:.4f}'.format(p_2d_proj_rpc_ba[0], p_2d_proj_rpc_ba[1]))

                print('Finished checking image {}'.format(im_idx))
                
        print('\nBundle adjusted RPCs successfully saved!\n')
        
    
    def visualize_feature_track(self, feature_track_index=None, verbose=True):
        
        
        
        from bundle_adjust.ba_triangulation import initialize_3d_points
        pts_3d = initialize_3d_points(self.input_P, self.C, self.pairs_to_triangulate, self.cam_model)
        
        pts_3d_ba_available = False
        if self.pts_3d_ba is not None:
            pts_3d_ba = initialize_3d_points(self.P_crop_ba, self.C, self.pairs_to_triangulate, self.cam_model)
            pts_3d_ba_available = True
        
        n_img = self.n_adj + self.n_new
        hC, wC = self.C.shape
        
        true_where_track = np.sum(np.invert(np.isnan(self.C[np.arange(0, hC, 2), :]))[-self.n_new:]*1,axis=0).astype(bool) 
        
        if feature_track_index is None:
            feature_track_index = np.random.choice(np.arange(0, wC)[true_where_track])
        p_ind = feature_track_index
        im_ind = [k for k, j in enumerate(range(n_img)) if not np.isnan(self.C[j*2,p_ind])]

        reprojection_error, reprojection_error_ba  = [], []
        cont = -1

        print('Displaying feature track with index {}, length {}\n'.format(p_ind, len(im_ind)))
        
        for i in im_ind:   
            cont += 1

            p_2d_gt = self.C[(i*2):(i*2+2),p_ind]

            proj = self.input_P[i] @ np.expand_dims(np.hstack((pts_3d[p_ind,:], np.ones(1))), axis=1)
            p_2d_proj = proj[0:2,:] / proj[-1,-1]  # col, row
            
            if pts_3d_ba_available:
                proj = self.P_crop_ba[i] @ np.expand_dims(np.hstack((pts_3d_ba[p_ind,:], np.ones(1))), axis=1)
                p_2d_proj_ba = proj[0:2,:] / proj[-1,-1]

            if cont == 0 and pts_3d_ba_available and verbose:
                print('3D location (initial)  :', pts_3d[p_ind,:].ravel())
                print('3D location (after BA) :', pts_3d_ba[p_ind,:].ravel(), '\n')

            if verbose:
                print(' ----> Real 2D loc in im', i, ' (yellow) = ', p_2d_gt)
                print(' ----> Proj 2D loc in im', i, ' before BA (red) = ', p_2d_proj.ravel())
                if pts_3d_ba_available:
                    print(' ----> Proj 2D loc in im', i, ' after  BA (green) = ', p_2d_proj_ba.ravel())
            current_reproj_err = np.sum(abs(p_2d_proj.ravel() - p_2d_gt))
            reprojection_error.append(current_reproj_err)
            
            if verbose:
                print('              Reprojection error beofre BA:', current_reproj_err)
            if pts_3d_ba_available:
                current_reproj_err = np.sum(abs(p_2d_proj_ba.ravel() - p_2d_gt))
                reprojection_error_ba.append(current_reproj_err)
                if verbose:
                    print('              Reprojection error after  BA:', current_reproj_err)

            if verbose:
                fig = plt.figure(figsize=(10,20))
                plt.imshow(self.input_seq[i], cmap="gray")
                plt.plot(*p_2d_gt, "yo")
                plt.plot(*p_2d_proj, "ro")
                if pts_3d_ba_available:
                    plt.plot(*p_2d_proj_ba, "go")
                plt.show()
            
        print('Mean reprojection error before BA: {}'.format(np.mean(reprojection_error)))
        if pts_3d_ba_available:
            print('Mean reprojection error after BA: {}'.format(np.mean(reprojection_error_ba)))
            
    
    def compute_reproj_err_per_image(self, im_idx):
    
        # pick all points visible in the selected image
        pts_gt = self.C[(im_idx*2):(im_idx*2+2),~np.isnan(self.C[im_idx*2,:])].T

        pts_3d_before = self.pts_3d[~np.isnan(self.C[im_idx*2,:]),:]
        pts_3d_after = self.pts_3d_ba[~np.isnan(self.C[im_idx*2,:]),:]

        # reprojections before bundle adjustment
        proj = self.input_P[im_idx] @ np.hstack((pts_3d_before, np.ones((pts_3d_before.shape[0],1)))).T
        pts_reproj_before = (proj[:2,:]/proj[-1,:]).T
        #pts_reproj_before = pts_reproj_before[::-1]

        # reprojections after bundle adjustment
        proj = self.P_crop_ba[im_idx] @ np.hstack((pts_3d_after, np.ones((pts_3d_after.shape[0],1)))).T
        pts_reproj_after = (proj[:2,:]/proj[-1,:]).T

        err_before = np.linalg.norm(pts_reproj_before - pts_gt, axis=1)
        err_after = np.linalg.norm(pts_reproj_after - pts_gt, axis=1)
        
        return err_before, err_after
    
    
    def analyse_reproj_err_particular_image(self, im_idx, plot_features=False):
        
        err_before, err_after = self.compute_reproj_err_per_image(im_idx)
        
        print('image {}, mean abs reproj error before BA: {:.4f}'.format(im_idx, np.mean(err_before)))
        print('image {}, mean abs reproj error after  BA: {:.4f}'.format(im_idx, np.mean(err_after)))

        # reprojection error histograms for the selected image
        fig = plt.figure(figsize=(10,3))
        ax1 = fig.add_subplot(121)
        ax2 = fig.add_subplot(122)
        ax1.title.set_text('Reprojection error before BA')
        ax2.title.set_text('Reprojection error after  BA')
        ax1.hist(err_before, bins=40); 
        ax2.hist(err_after, bins=40);
        plt.show()      

        if plot_features:
        # warning: this is slow...
        # Green crosses represent the observations from feature tracks seen in the image, 
        # red vectors are the distance to the reprojected point locations. 
            fig = plt.figure(figsize=(20,6))
            ax1 = fig.add_subplot(121)
            ax2 = fig.add_subplot(122)
            ax1.title.set_text('Before BA')
            ax2.title.set_text('After  BA')
            ax1.imshow((self.input_seq[im_idx]), cmap="gray")
            ax2.imshow((self.input_seq[im_idx]), cmap="gray")
            for k in range(min(1000,pts_gt.shape[0])):
                # before bundle adjustment
                ax1.plot([pts_gt[k,0], pts_reproj_before[k,0] ], [pts_gt[k,1], pts_reproj_before[k,1] ], 'r-', lw=3)
                ax1.plot(*pts_gt[k], 'yx')
                # after bundle adjustment
                #ax2.plot([pts_gt[k,0], pts_reproj_after[k,0] ], [pts_gt[k,1], pts_reproj_after[k,1]], 'r-', lw=3)
                ax2.plot(*pts_gt[k], 'yx')
            plt.show()
        
    def save_crops(self, output_dir, img_indices=None):
        
        images_dir = os.path.join(output_dir, 'images')
        os.makedirs(images_dir, exist_ok=True)
        
        if img_indices is None:
            n_img = self.n_adj + self.n_new
            img_indices = np.arange(n_img)
            
        for im_idx in img_indices:
            f_id = os.path.splitext(os.path.basename(self.myimages[im_idx]))[0]
            Image.fromarray(self.input_seq[im_idx]).save(os.path.join(images_dir, '{}.tif'.format(f_id)))

        print('\nImage crops were saved at {}\n'.format(images_dir))
    
    
    def save_sift_kp_as_svg(self, output_dir, img_indices=None):
        
        sift_dir = os.path.join(output_dir, 'features/sift_all_kp')
        os.makedirs(sift_dir, exist_ok=True)
        
        if img_indices is None:
            n_img = self.n_adj + self.n_new
            img_indices = np.arange(n_img)
            
        for im_idx in img_indices:
            
            f_id = os.path.splitext(os.path.basename(self.myimages[im_idx]))[0]
            h,w = self.input_seq[im_idx].shape
            svg_fname = os.path.join(sift_dir, '{}.svg'.format(f_id))
            ba_utils.save_pts2d_as_svg(svg_fname, self.features[im_idx]['kp'], w, h, 'green')

        print('\nSIFT keypoints were saved at {}\n'.format(sift_dir))
    
    
    def save_feature_tracks_as_svg(self, output_dir, img_indices=None, save_reprojected=True):
        
        self.save_crops(output_dir, img_indices)
        self.save_sift_kp_as_svg(output_dir, img_indices)
        
        before_dir = os.path.join(output_dir, 'features/tracks_reproj_before')
        after_dir = os.path.join(output_dir, 'features/tracks_reproj_after')
        original_dir = os.path.join(output_dir, 'features/tracks_sift')
        os.makedirs(before_dir, exist_ok=True)
        os.makedirs(after_dir, exist_ok=True)
        os.makedirs(original_dir, exist_ok=True)
                
        if img_indices is None:
            n_img = self.n_adj + self.n_new
            img_indices = np.arange(n_img)
        
        for im_idx in img_indices:
           
            f_id = os.path.splitext(os.path.basename(self.myimages[im_idx]))[0]
            h,w = self.input_seq[im_idx].shape
            
            svg_fname_o = os.path.join(original_dir, '{}.svg'.format(f_id))
            svg_fname_b = os.path.join(before_dir, '{}.svg'.format(f_id))
            svg_fname_a = os.path.join(after_dir, '{}.svg'.format(f_id))
        
            P_before = self.input_P[im_idx]
            P_after = self.P_crop_ba[im_idx]
            
            # pick all points visible in the selected image
            pts2d = self.C[(im_idx*2):(im_idx*2+2),~np.isnan(self.C[im_idx*2,:])].T
            pts3d_before = self.pts_3d[~np.isnan(self.C[im_idx*2,:]),:]
            pts3d_after = self.pts_3d_ba[~np.isnan(self.C[im_idx*2,:]),:]
            n_pts = pts3d_before.shape[0]
            
            # reprojections before bundle adjustment
            proj = P_before @ np.hstack((pts3d_before, np.ones((n_pts,1)))).T
            pts_reproj_before = (proj[:2,:]/proj[-1,:]).T

            # reprojections after bundle adjustment
            proj = P_after @ np.hstack((pts3d_after, np.ones((n_pts,1)))).T
            pts_reproj_after = (proj[:2,:]/proj[-1,:]).T

            err_before = np.sum(abs(pts_reproj_before - pts2d), axis=1)
            err_after = np.sum(abs(pts_reproj_after - pts2d), axis=1)
            
            # draw pts on svg
            ba_utils.save_pts2d_as_svg(svg_fname_o, pts2d, w, h, 'green')
            ba_utils.save_pts2d_as_svg(svg_fname_b, pts_reproj_before, w, h, 'red')
            ba_utils.save_pts2d_as_svg(svg_fname_a, pts_reproj_after, w, h, 'yellow')
            

        print('\nFeature tracks and their reprojection were saved at {}\n'.format(output_dir))
    
    def get_number_of_matches_between_groups_of_views(self, img_indices_g1, img_indices_g2):
        
        img_indices_g1_s = sorted(img_indices_g1)
        img_indices_g2_s = sorted(img_indices_g2)
        n_matches = 0
        n_matches_inside_aoi = 0
        for im1 in img_indices_g1:
            for im2 in img_indices_g2:
                obs_im1 = 1*np.invert(np.isnan(self.C[2*im1,:]))
                obs_im2 = 1*np.invert(np.isnan(self.C[2*im2,:]))
                true_if_obs_seen_in_both_cams = np.sum(np.vstack((obs_im1, obs_im2)), axis=0) == 2
                n_matches += np.sum(1*true_if_obs_seen_in_both_cams)
                
                if self.input_masks is not None:
                    tmp = np.zeros(self.C.shape[1])
                    pts2d_colrow = (self.C[(2*im1):(2*im1+2),:][:,obs_im1.astype(bool)].T).astype(np.int)
                    tmp[obs_im1.astype(bool)] = 1*(self.input_masks[im1][pts2d_colrow[:,1], pts2d_colrow[:,0]] > 0)
                    true_if_obs_inside_aoi = tmp.astype(bool)
                    n_matches_inside_aoi += np.sum(1*np.logical_and(true_if_obs_seen_in_both_cams, \
                                                                    true_if_obs_inside_aoi))
                else:
                    n_matches_inside_aoi += None
        
        return n_matches, n_matches_inside_aoi
    
    def get_n_matches_within_group_of_views(self, img_indices_g1):
        
        img_indices_g1_s = sorted(img_indices_g1)
        n_matches = 0
        n_matches_inside_aoi = 0
        for im1 in img_indices_g1_s:
            for im2 in np.array(img_indices_g1_s[im1+1:]).tolist():
                obs_im1 = 1*np.invert(np.isnan(self.C[2*im1,:]))
                obs_im2 = 1*np.invert(np.isnan(self.C[2*im2,:]))
                true_if_obs_seen_in_both_cams = np.sum(np.vstack((obs_im1, obs_im2)), axis=0) == 2
                n_matches += np.sum(np.sum(np.vstack((obs_im1, obs_im2)), axis=0) == 2)
                
                if self.input_masks is not None:
                    tmp = np.zeros(self.C.shape[1])
                    pts2d_colrow = (self.C[(2*im1):(2*im1+2),:][:,obs_im1.astype(bool)].T).astype(np.int)
                    tmp[obs_im1.astype(bool)] = 1*(self.input_masks[im1][pts2d_colrow[:,1], pts2d_colrow[:,0]] > 0)
                    true_if_obs_inside_aoi = tmp.astype(bool)
                    n_matches_inside_aoi += np.sum(1*np.logical_and(true_if_obs_seen_in_both_cams, \
                                                                    true_if_obs_inside_aoi))
        return n_matches, n_matches_inside_aoi
    
    
    def get_n_tracks_within_group_of_views(self, img_indices_g1):
        
        # compute tracks within the specified cameras
        img_indices = sorted(img_indices_g1)
        true_if_track = (np.sum(~(np.isnan(self.C[np.arange(0,self.C.shape[0],2)[img_indices],:])),axis=0)>1).astype(bool)
        n_tracks = np.sum(1*true_if_track)
        
        if self.input_masks is not None:
        
            # compute tracks inside AOI within the specified cameras
            n_tracks_inside_aoi = 0
            n_tracks_in_C = self.C.shape[1]
            n_cam_in_C = int(self.C.shape[0]/2)
            true_if_cam = np.zeros(n_cam_in_C).astype(bool)
            true_if_cam[img_indices] = True
            true_if_cam = np.repeat(true_if_cam, 2)
            true_if_cam_2d = np.repeat(np.array([true_if_cam]), n_tracks_in_C, axis=0).T # same size as C
            true_if_track_2d =  np.repeat(np.array([true_if_track]), n_cam_in_C * 2, axis=0)
            cam_indices = np.repeat(np.array([np.arange(self.C.shape[0])/2]),n_tracks_in_C,axis=0).T
            cam_indices = cam_indices.astype(int).astype(float) # int removes decimals, float is necessary to use nan
            cam_indices[np.invert(true_if_track_2d * true_if_cam_2d)] = np.nan
            cam_indices[np.isnan(self.C)] = np.nan

            # take the first camera where the track is visible
            cam_indices_to_get_pts2d = np.nanmin(cam_indices[:, true_if_track],axis=0).astype(int) 
            track_indices_to_get_pts2d = np.arange(n_tracks_in_C)[true_if_track].astype(int)

            max_col, max_row = 0, 0
            for track_idx, cam_idx in zip(track_indices_to_get_pts2d, cam_indices_to_get_pts2d):
                col, row = self.C[2*cam_idx, track_idx].astype(int), self.C[2*cam_idx + 1, track_idx].astype(int)
                n_tracks_inside_aoi += 1*(self.input_masks[cam_idx][row, col] > 0)
        else:
            print('ba_pipeline.get_number_of_tracks_within_group_of_views cannot get number of tracks inside the aoi because aoi masks are not available !')
        
        return n_tracks, n_tracks_inside_aoi
         
    def approximate_rpcs_as_proj_matrices(self):
        input_crops = [{'crop': i, 'col0':o['col0'], 'row0':o['row0']} for i, o in zip(self.input_seq, self.crop_offsets)]
        
        return ba_core.approximate_rpcs_as_proj_matrices(self.input_rpcs.copy(),
                                                                 input_crops.copy(), self.aoi, self.cam_model)
   

    def get_image_weights(self):
        
        from feature_tracks import ft_ranking
        
        # get base node of a group of views after running bundle adjustment
        
        C_reproj = ft_ranking.reprojection_error_from_C(self.C, self.P_crop_ba,
                                                        self.pairs_to_triangulate,
                                                        self.cam_model)
        
        cam_weights = ft_ranking.compute_camera_weights(self.C, C_reproj)[self.n_adj:]
        
        return cam_weights
    
    
    def run(self):
        
        # compute feature tracks
        self.compute_feature_tracks()
            
        # run bundle adjustment
        self.define_ba_parameters()
        self.run_ba_softL1()
        #self.clean_outlier_obs()
        #self.run_ba_L2()
        
        # save output
        self.save_corrected_matrices()
        self.save_corrected_rpcs()
        
        