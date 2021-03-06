# Licensed under a 3-clause BSD style license - see LICENSE.rst
from __future__ import absolute_import, division, print_function, unicode_literals
import abc
import copy
import re
from collections import OrderedDict
import numpy as np
from ..extern import six
from astropy.utils.misc import InheritDocstrings
from astropy.io import fits
from astropy import units as u
from astropy.coordinates import SkyCoord
from ..utils.scripts import make_path
from .utils import find_hdu, find_bands_hdu

__all__ = [
    'MapCoord',
    'MapGeom',
    'MapAxis',
]


def make_axes(axes_in, conv):
    """Make a sequence of `~MapAxis` objects."""
    if axes_in is None:
        return []

    axes_out = []
    for i, ax in enumerate(axes_in):
        if isinstance(ax, np.ndarray):
            ax = MapAxis(ax)

        if conv in ['fgst-ccube', 'fgst-template']:
            ax.name = 'energy'
        elif ax.name == '':
            ax.name = 'axis%i' % i

        axes_out += [ax]

    return axes_out


def make_axes_cols(axes, axis_names=None):
    """Make FITS table columns for map axes.

    Parameters
    ----------
    axes : list
        Python list of `MapAxis` objects

    Returns
    -------
    cols : list
        Python list of `~astropy.io.fits.Column`
    """
    size = np.prod([ax.nbin for ax in axes])
    chan = np.arange(0, size)
    cols = [fits.Column('CHANNEL', 'I', array=chan), ]

    if axis_names is None:
        axis_names = [ax.name for ax in axes]
    axis_names = [_.upper() for _ in axis_names]

    axes_ctr = np.meshgrid(*[ax.center for ax in axes])
    axes_min = np.meshgrid(*[ax.edges[:-1] for ax in axes])
    axes_max = np.meshgrid(*[ax.edges[1:] for ax in axes])

    for i, (ax, name) in enumerate(zip(axes, axis_names)):

        if name == 'ENERGY':
            colnames = ['ENERGY', 'E_MIN', 'E_MAX']
        elif name == 'TIME':
            colnames = ['TIME', 'T_MIN', 'T_MAX']
        else:
            s = 'AXIS%i' % i if name == '' else name
            colnames = [s, s + '_MIN', s + '_MAX']

        for colname, v in zip(colnames, [axes_ctr, axes_min, axes_max]):
            array = np.ravel(v[i])
            unit = ax.unit.to_string()
            cols.append(fits.Column(colname, 'E', array=array, unit=unit))

    return cols


def find_and_read_bands(hdu, header=None):
    """Read and returns the map axes from a BANDS table.

    Parameters
    ----------
    hdu : `~astropy.io.fits.BinTableHDU`
        The BANDS table HDU.
    header : `~astropy.io.fits.Header`
        Header

    Returns
    -------
    axes : list of `~MapAxis`
        List of axis objects.
    """
    if hdu is None:
        return []

    axes = []
    axis_cols = []
    if hdu.name == 'ENERGIES':
        axis_cols = [['ENERGY']]
    elif hdu.name == 'EBOUNDS':
        axis_cols = [['E_MIN', 'E_MAX']]
    else:
        for i in range(5):
            if 'AXCOLS%i' % i in hdu.header:
                axis_cols += [hdu.header['AXCOLS%i' % i].split(',')]

    for i, cols in enumerate(axis_cols):

        if 'ENERGY' in cols or 'E_MIN' in cols:
            name = 'energy'
        elif re.search('(.+)_MIN', cols[0]):
            name = re.search('(.+)_MIN', cols[0]).group(1)
        else:
            name = cols[0]

        unit = hdu.data.columns[cols[0]].unit
        if unit is None and header is not None:
            unit = header.get('CUNIT%i' % (3 + i), '')

        if len(cols) == 2:
            xmin = np.unique(hdu.data.field(cols[0]))
            xmax = np.unique(hdu.data.field(cols[1]))
            nodes = np.append(xmin, xmax[-1])
            axes.append(MapAxis(nodes, name=name, unit=unit))
        else:
            nodes = np.unique(hdu.data.field(cols[0]))
            axes.append(MapAxis.from_nodes(nodes, name=name, unit=unit))

    return axes


def get_shape(param):
    if param is None:
        return tuple()

    if not isinstance(param, tuple):
        param = [param]

    return max([np.array(p, ndmin=1).shape for p in param])


def coordsys_to_frame(coordsys):
    if coordsys in ['CEL', 'C']:
        return 'icrs'
    elif coordsys in ['GAL', 'G']:
        return 'galactic'
    else:
        raise ValueError('Unrecognized coordinate system: {}'.format(coordsys))


def skycoord_to_lonlat(skycoord, coordsys=None):
    """

    Returns
    -------
    lon : `~numpy.ndarray`
        Longitude in degrees.

    lat : `~numpy.ndarray`
        Latitude in degrees.

    frame : str
        Name of coordinate frame.
    """

    if coordsys in ['CEL', 'C']:
        skycoord = skycoord.transform_to('icrs')
    elif coordsys in ['GAL', 'G']:
        skycoord = skycoord.transform_to('galactic')

    frame = skycoord.frame.name
    if frame in ['icrs', 'fk5']:
        return skycoord.ra.deg, skycoord.dec.deg, frame
    elif frame in ['galactic']:
        return skycoord.l.deg, skycoord.b.deg, frame
    else:
        raise ValueError('Unrecognized SkyCoord frame: {}'.format(frame))


def lonlat_to_skycoord(lon, lat, coordsys):
    return SkyCoord(lon, lat, frame=coordsys_to_frame(coordsys), unit='deg')


def pix_tuple_to_idx(pix, copy=False):
    """Convert a tuple of pixel coordinate arrays to a tuple of pixel
    indices.

    Pixel coordinates are rounded to the closest integer value.

    Parameters
    ----------
    pix : tuple
        Tuple of pixel coordinates with one element for each dimension.

    copy : bool
        Flag to set whether a copy or view is returned.

    Returns
    -------
    idx : `~numpy.ndarray`
        Array of pixel indices.
    """
    idx = []
    for p in pix:
        p = np.array(p, copy=copy, ndmin=1)
        if np.issubdtype(p.dtype, np.integer):
            idx += [p]
        else:
            p_idx = np.rint(p).astype(int)
            p_idx[~np.isfinite(p)] = -1
            idx += [p_idx]

    return tuple(idx)


def axes_pix_to_coord(axes, pix):
    """Perform pixel to axis coordinates for a list of `~MapAxis`
    objects.

    Parameters
    ----------
    axes : list
        List of `~MapAxis`.

    pix : tuple
        Tuple of pixel coordinates.
    """
    coords = []
    for ax, t in zip(axes, pix):
        coords += [ax.pix_to_coord(t)]

    return coords


def coord_to_idx(edges, x, clip=False):
    """Convert axis coordinates ``x`` to bin indices.

    Returns -1 for values below/above the lower/upper edge.
    """
    x = np.array(x, ndmin=1)
    ibin = np.digitize(x, edges) - 1

    if clip:
        ibin[x < edges[0]] = 0
        ibin[x > edges[-1]] = len(edges) - 1
    else:
        ibin[x > edges[-1]] = -1

    ibin[~np.isfinite(x)] = -1
    return ibin


def bin_to_val(edges, bins):
    ctr = 0.5 * (edges[1:] + edges[:-1])
    return ctr[bins]


def coord_to_pix(edges, coord, interp='lin'):
    """Convert axis coordinates to pixel coordinates using the chosen
    interpolation scheme."""
    from scipy.interpolate import interp1d

    if interp == 'log':
        fn = np.log
    elif interp == 'lin':
        def fn(t):
            return t
    elif interp == 'sqrt':
        fn = np.sqrt
    else:
        raise ValueError('Invalid interp: {}'.format(interp))

    interp_fn = interp1d(
        fn(edges),
        np.arange(len(edges)).astype(float),
        fill_value='extrapolate',
    )

    return interp_fn(fn(coord))


def pix_to_coord(edges, pix, interp='lin'):
    """Convert pixel coordinates to grid coordinates using the chosen
    interpolation scheme."""
    from scipy.interpolate import interp1d

    if interp == 'log':
        fn0 = np.log
        fn1 = np.exp
    elif interp == 'lin':
        def fn0(t):
            return t

        def fn1(t):
            return t
    elif interp == 'sqrt':
        fn0 = np.sqrt

        def fn1(t):
            return np.power(t, 2)
    else:
        raise ValueError('Invalid interp: {}'.format(interp))

    interp_fn = interp1d(
        np.arange(len(edges)).astype(float),
        fn0(edges),
        fill_value='extrapolate',
    )

    return fn1(interp_fn(pix))


class MapAxis(object):
    """Class representing an axis of a map.

    Provides methods for
    transforming to/from axis and pixel coordinates.  An axis is
    defined by a sequence of node values that lie at the center of
    each bin.  The pixel coordinate at each node is equal to its index
    in the node array (0, 1, ..).  Bin edges are offset by 0.5 in
    pixel coordinates from the nodes such that the lower/upper edge of
    the first bin is (-0.5,0.5).

    Parameters
    ----------
    nodes : `~numpy.ndarray`
        Array of node values.  These will be interpreted as either bin
        edges or centers according to ``node_type``.
    interp : str
        Interpolation method used to transform between axis and pixel
        coordinates.  Valid options are 'log', 'lin', and 'sqrt'.
    name : str
        Axis name
    node_type : str
        Flag indicating whether coordinate nodes correspond to pixel
        edges (node_type = 'edge') or pixel centers (node_type =
        'center').  'center' should be used where the map values are
        defined at a specific coordinate (e.g. differential
        quantities). 'edge' should be used where map values are
        defined by an integral over coordinate intervals (e.g. a
        counts histogram).
    unit : str
        String specifying the data units.
    """

    # TODO: Add methods to faciliate FITS I/O.
    # TODO: Cache an interpolation object?

    def __init__(self, nodes, interp='lin', name='',
                 node_type='edge', unit=''):
        self._name = name
        self._interp = interp
        self._nodes = nodes
        self._node_type = node_type
        self._unit = u.Unit('' if unit is None else unit)

        # Set axis type from its unit
        if self._unit.is_equivalent("eV"):
            self._type = 'energy'
        elif self._unit.is_equivalent("s"):
            self._type = 'time'
        else:
            self._type = 'any'

        # Set pixel coordinate of first node
        if node_type == 'edge':
            self._pix_offset = -0.5
            nbin = len(nodes) - 1
        elif node_type == 'center':
            self._pix_offset = 0.0
            nbin = len(nodes)
        else:
            raise ValueError('Invalid node type: {}'.format(node_type))

        pix = np.arange(nbin, dtype=float)
        self._center = self.pix_to_coord(pix)
        pix = np.arange(nbin + 1, dtype=float) - 0.5
        self._bin_edges = self.pix_to_coord(pix)

    def __eq__(self, other):
        if isinstance(other, self.__class__):
            return (np.allclose(self._nodes, other._nodes) and
                    self._node_type == other._node_type and
                    self._interp == other._interp and
                    self._unit == other._unit)
        return NotImplemented

    def __ne__(self, other):
        if isinstance(other, self.__class__):
            return not self.__eq__(other)
        return NotImplemented

    @property
    def name(self):
        """Name of the axis."""
        return self._name

    @name.setter
    def name(self, val):
        self._name = val

    @property
    def edges(self):
        """Return array of bin edges."""
        return self._bin_edges

    @property
    def center(self):
        """Return array of bin centers."""
        return self._center

    @property
    def nbin(self):
        """Return number of bins."""
        return len(self._bin_edges) - 1

    @property
    def node_type(self):
        """Return node type ('center' or 'edge')."""
        return self._node_type

    @property
    def unit(self):
        """Return coordinate axis unit."""
        return self._unit

    @property
    def type(self):
        """Return coordinate axis type."""
        return self._type

    @classmethod
    def from_bounds(cls, lo_bnd, hi_bnd, nbin, **kwargs):
        """Generate an axis object from a lower/upper bound and number of bins.

        If node_type = 'edge' then bounds correspond to the
        lower and upper bound of the first and last bin.  If node_type
        = 'center' then bounds correspond to the centers of the first
        and last bin.

        Parameters
        ----------
        lo_bnd : float
            Lower bound of first axis bin.
        hi_bnd : float
            Upper bound of last axis bin.
        nbin : int
            Number of bins.
        interp : {'lin', 'log', 'sqrt'}
            Interpolation method used to transform between axis and pixel
            coordinates.  Default: 'lin'.
        """
        interp = kwargs.setdefault('interp', 'lin')
        node_type = kwargs.setdefault('node_type', 'edge')

        if node_type == 'edge':
            nnode = nbin + 1
        elif node_type == 'center':
            nnode = nbin
        else:
            raise ValueError('Invalid node type: {}'.format(node_type))

        if interp == 'lin':
            nodes = np.linspace(lo_bnd, hi_bnd, nnode)
        elif interp == 'log':
            nodes = np.exp(np.linspace(np.log(lo_bnd),
                                       np.log(hi_bnd), nnode))
        elif interp == 'sqrt':
            nodes = np.linspace(lo_bnd ** 0.5,
                                hi_bnd ** 0.5, nnode) ** 2.0
        else:
            raise ValueError('Invalid interp: {}'.format(interp))

        return cls(nodes, **kwargs)

    @classmethod
    def from_nodes(cls, nodes, **kwargs):
        """Generate an axis object from a sequence of nodes (bin centers).

        This will create a sequence of bins with edges half-way
        between the node values.  This method should be used to
        construct an axis where the bin center should lie at a
        specific value (e.g. a map of a continuous function).

        Parameters
        ----------
        nodes : `~numpy.ndarray`
            Axis nodes (bin center).
        interp : {'lin', 'log', 'sqrt'}
            Interpolation method used to transform between axis and pixel
            coordinates.  Default: 'lin'.
        """
        nodes = np.array(nodes, ndmin=1)
        if len(nodes) < 1:
            raise ValueError('Nodes array must have at least one element.')

        return cls(nodes, node_type='center', **kwargs)

    @classmethod
    def from_edges(cls, edges, **kwargs):
        """Generate an axis object from a sequence of bin edges.

        This method should be used to construct an axis where the bin
        edges should lie at specific values (e.g. a histogram).  The
        number of bins will be one less than the number of edges.

        Parameters
        ----------
        edges : `~numpy.ndarray`
            Axis bin edges.
        interp : {'lin', 'log', 'sqrt'}
            Interpolation method used to transform between axis and pixel
            coordinates.  Default: 'lin'.
        """
        if len(edges) < 2:
            raise ValueError('Edges array must have at least two elements.')

        return cls(edges, node_type='edge', **kwargs)

    def pix_to_coord(self, pix):
        """Transform from pixel to axis coordinates.

        Parameters
        ----------
        pix : `~numpy.ndarray`
            Array of pixel coordinate values.

        Returns
        -------
        coord : `~numpy.ndarray`
            Array of axis coordinate values.
        """
        pix = pix - self._pix_offset
        return pix_to_coord(self._nodes, pix, interp=self._interp)

    def coord_to_pix(self, coord):
        """Transform from axis to pixel coordinates.

        Parameters
        ----------
        coord : `~numpy.ndarray`
            Array of axis coordinate values.

        Returns
        -------
        pix : `~numpy.ndarray`
            Array of pixel coordinate values.
        """
        pix = coord_to_pix(self._nodes, coord, interp=self._interp)
        return np.array(pix + self._pix_offset, ndmin=1)

    def coord_to_idx(self, coord, clip=False):
        """Transform from axis coordinate to bin index.

        Parameters
        ----------
        coord : `~numpy.ndarray`
            Array of axis coordinate values.
        clip : bool
            Choose whether to clip the index to the valid range of the
            axis.  If false then indices for values outside the axis
            range will be set -1.

        Returns
        -------
        idx : `~numpy.ndarray`
            Array of bin indices.
        """
        return coord_to_idx(self.edges, coord, clip)

    def coord_to_idx_interp(self, coord):
        """Compute indices of two nearest bins to the given coordinate.

        Parameters
        ----------
        coord : `~numpy.ndarray`
            Array of axis coordinate values.
        """
        return (coord_to_idx(self.center[:-1], coord, clip=True),
                coord_to_idx(self.center[:-1], coord, clip=True) + 1,)

    def slice(self, idx):
        """Create a new axis object by extracting a slice from this axis.

        Parameters
        ----------
        idx : slice
            Slice object selecting a subselection of the axis.

        Returns
        -------
        axis : `~MapAxis`
            Sliced axis objected.
        """
        center = self.center[idx]
        idx = self.coord_to_idx(center)
        # For edge nodes we need to keep N+1 nodes
        if self._node_type == 'edge':
            idx = tuple(list(idx) + [1 + idx[-1]])
        nodes = self._nodes[(idx,)]
        return MapAxis(nodes, interp=self._interp, name=self._name,
                       node_type=self._node_type, unit=self._unit)

    def __repr__(self):
        str_ = self.__class__.__name__
        str_ += "\n\n"
        str_ += "\tname     : {}\n".format(self.name)
        str_ += "\ttype     : {}\n".format(self.type)
        str_ += "\tunit     : {}\n".format(self.unit)
        str_ += "\tnbins    : {}\n".format(self.nbin)
        str_ += "\tnode type: {}\n".format(self.node_type)
        str_ += "\tedge min : {:.1e} {}\n".format(self.edges.min(), self.unit)
        str_ += "\tedge max : {:.1e} {}\n".format(self.edges.max(), self.unit)
        str_ += "\tinterp   : {}\n".format(self._interp)
        return str_


class MapCoord(object):
    """Represents a sequence of n-dimensional map coordinates.

    Contains coordinates for 2 spatial dimensions and an arbitrary
    number of additional non-spatial dimensions.

    For further information see :ref:`mapcoord`.

    Parameters
    ----------
    data : `~collections.OrderedDict` of `~numpy.ndarray`
        Dictionary of coordinate arrays.
    coordsys : {'CEL', 'GAL', None}
        Spatial coordinate system.  If None then the coordinate system
        will be set to the native coordinate system of the geometry.
    copy : bool
        Make copies of the input arrays?
        If False then this object will store views.
    match_by_name : bool
        Match coordinates to axes by name?
        If false coordinates will be matched by index.
    """

    def __init__(self, data, coordsys=None, copy=False, match_by_name=True):

        if 'lon' not in data or 'lat' not in data:
            raise ValueError("data dictionary must contain axes named 'lon' and 'lat'.")

        self._data = OrderedDict([
            (k, np.array(v, ndmin=1, copy=copy))
            for k, v in data.items()
        ])
        vals = np.broadcast_arrays(*self._data.values())
        self._data = OrderedDict(zip(self._data.keys(), vals))
        self._coordsys = coordsys
        self._match_by_name = match_by_name

    def __getitem__(self, key):
        if isinstance(key, six.string_types):
            return self._data[key]
        else:
            return list(self._data.values())[key]

    def __iter__(self):
        return iter(self._data.values())

    @property
    def ndim(self):
        """Number of dimensions."""
        return len(self._data)

    @property
    def shape(self):
        """Coordinate array shape."""
        return self[0].shape

    @property
    def size(self):
        return self[0].size

    @property
    def lon(self):
        """Longitude coordinate in degrees."""
        return self._data['lon']

    @property
    def lat(self):
        """Latitude coordinate in degrees."""
        return self._data['lat']

    @property
    def coordsys(self):
        """Coordinate system (str)"""
        return self._coordsys

    @property
    def match_by_name(self):
        """Boolean flag indicating whether axis lookup should be performed by
        name (True) or index (False).
        """
        return self._match_by_name

    @property
    def skycoord(self):
        return SkyCoord(self.lon, self.lat, unit='deg',
                        frame=coordsys_to_frame(self.coordsys))

    @classmethod
    def _from_lonlat(cls, coords, coordsys=None, copy=False):
        """Create a `~MapCoord` from a tuple of coordinate vectors.

        The first two elements of the tuple should be longitude and latitude in degrees.

        Parameters
        ----------
        coords : tuple
            Tuple of `~numpy.ndarray`.

        Returns
        -------
        coord : `~MapCoord`
            A coordinates object.
        """
        if isinstance(coords, (list, tuple)):
            coords_dict = OrderedDict([('lon', coords[0]),
                                       ('lat', coords[1])])
            for i, c in enumerate(coords[2:]):
                coords_dict['axis{}'.format(i)] = c
        else:
            raise ValueError('Unrecognized input type.')

        return cls(coords_dict, coordsys=coordsys, copy=copy,
                   match_by_name=False)

    @classmethod
    def _from_skycoord(cls, coords, coordsys=None, copy=False):
        """Create from vector of `~astropy.coordinates.SkyCoord`.

        Parameters
        ----------
        coords : tuple
            Coordinate tuple with first element of type
            `~astropy.coordinates.SkyCoord`.
        coordsys : {'CEL', 'GAL', None}
            Spatial coordinate system of output `~MapCoord` object.
            If None the coordinate system will be set to the frame of
            the `~astropy.coordinates.SkyCoord` object.
        """
        skycoord = coords[0]
        if skycoord.frame.name in ['icrs', 'fk5']:
            coords = (skycoord.ra.deg, skycoord.dec.deg) + coords[1:]
            coords = cls._from_lonlat(coords, coordsys='CEL', copy=copy)
        elif skycoord.frame.name in ['galactic']:
            coords = (skycoord.l.deg, skycoord.b.deg) + coords[1:]
            coords = cls._from_lonlat(coords, coordsys='GAL', copy=copy)
        else:
            raise ValueError(
                'Unrecognized coordinate frame: {}'.format(skycoord.frame.name))

        if coordsys is None:
            return coords
        else:
            return coords.to_coordsys(coordsys)

    @classmethod
    def _from_tuple(cls, coords, coordsys=None, copy=False):
        """Create from tuple of coordinate vectors."""
        if isinstance(coords[0], (list, np.ndarray)) or np.isscalar(coords[0]):
            return cls._from_lonlat(coords, coordsys=coordsys, copy=copy)
        elif isinstance(coords[0], SkyCoord):
            return cls._from_skycoord(coords, coordsys=coordsys, copy=copy)
        else:
            raise TypeError('Type not supported: {}'.format(type(coords)))

    @classmethod
    def _from_dict(cls, coords, coordsys=None, copy=False):
        """Create from a dictionary of coordinate vectors."""
        if 'lon' in coords and 'lat' in coords:
            return cls(coords, coordsys=coordsys, copy=copy)
        elif 'skycoord' in coords:
            coords_dict = OrderedDict()
            lon, lat, frame = skycoord_to_lonlat(
                coords['skycoord'], coordsys=coordsys)
            coords_dict['lon'] = lon
            coords_dict['lat'] = lat
            for k, v in coords.items():
                if k == 'skycoord':
                    continue
                coords_dict[k] = v
            return cls(coords_dict, coordsys=coordsys, copy=copy)
        else:
            raise ValueError("Dictionary must contain axes named 'lon'/'lat'"
                             "or 'skycoord'.")

    @classmethod
    def create(cls, data, coordsys=None, copy=False):
        """Create a new `~MapCoord` object.

        This method can be used to create either unnamed (with tuple input)
        or named (via dict input) axes.

        Parameters
        ----------
        data : `tuple`, `dict`, `~MapCoord` or `~astropy.coordinates.SkyCoord`
            Object containing coordinate arrays.
        coordsys : {'CEL', 'GAL', None}, optional
            Set the coordinate system for longitude and latitude.  If
            None longitude and latitude will be assumed to be in
            the coordinate system native to a given map geometry.
        copy : bool
            Make copies of the input coordinate arrays.  If False this
            object will store views.

        Examples
        --------
        >>> from astropy.coordinates import SkyCoord
        >>> from gammapy.maps import MapCoord

        >>> lon, lat = [1, 2], [2, 3]
        >>> skycoord = SkyCoord(lon, lat, unit='deg')
        >>> energy = [1000]
        >>> c = MapCoord.create((lon,lat))
        >>> c = MapCoord.create((skycoord,))
        >>> c = MapCoord.create((lon,lat,energy))
        >>> c = MapCoord.create(dict(lon=lon,lat=lat))
        >>> c = MapCoord.create(dict(lon=lon,lat=lat,energy=energy))
        >>> c = MapCoord.create(dict(skycoord=skycoord,energy=energy))
        """
        if isinstance(data, cls):
            if data.coordsys is None or coordsys == data.coordsys:
                return data
            else:
                return data.to_coordsys(coordsys)
        elif isinstance(data, dict):
            return cls._from_dict(data, coordsys=coordsys, copy=copy)
        elif isinstance(data, (list, tuple)):
            return cls._from_tuple(data, coordsys=coordsys, copy=copy)
        elif isinstance(data, SkyCoord):
            return cls._from_skycoord((data,), coordsys=coordsys, copy=copy)
        else:
            raise TypeError('Unsupported input type: {}'.format(type(data)))

    def to_coordsys(self, coordsys):
        """Convert to a different coordinate frame.

        Parameters
        ----------
        coordsys : {'CEL', 'GAL'}
            Coordinate system, either Galactic ('GAL') or Equatorial ('CEL').

        Returns
        -------
        coords : `~MapCoord`
            A coordinates object.
        """
        if coordsys == self.coordsys:
            return copy.deepcopy(self)
        else:
            skycoord = lonlat_to_skycoord(self.lon, self.lat, self.coordsys)
            lon, lat, frame = skycoord_to_lonlat(skycoord, coordsys=coordsys)
            data = copy.deepcopy(self._data)
            data['lon'] = lon
            data['lat'] = lat
            return self.__class__(data, coordsys=coordsys,
                                  match_by_name=self._match_by_name)

    def apply_mask(self, mask):
        """Return a masked copy of this coordinate object.

        Parameters
        ----------
        mask : `~numpy.ndarray`
            Boolean mask.

        Returns
        -------
        coords : `~MapCoord`
            A coordinates object.
        """
        data = OrderedDict([(k, v[mask]) for k, v in self._data.items()])
        return self.__class__(data, self.coordsys,
                              match_by_name=self._match_by_name)

    def __repr__(self):
        str_ = self.__class__.__name__
        str_ += "\n\n"
        str_ += "\taxes    : {}\n".format(list(self._data.keys()))
        str_ += "\tshape   : {}\n".format(self.shape)
        str_ += "\tndim    : {}\n".format(self.ndim)
        str_ += "\tcoordsys: {}\n".format(self.coordsys)
        return str_


class MapGeomMeta(InheritDocstrings, abc.ABCMeta):
    pass


@six.add_metaclass(MapGeomMeta)
class MapGeom(object):
    """Base class for WCS and HEALPix geometries."""

    @property
    @abc.abstractmethod
    def is_allsky(self):
        pass

    @property
    @abc.abstractmethod
    def center_coord(self):
        pass

    @property
    @abc.abstractmethod
    def center_pix(self):
        pass

    @property
    @abc.abstractmethod
    def center_skydir(self):
        pass

    @classmethod
    def read(cls, filename, **kwargs):
        """Create a geometry object from a FITS file.

        Parameters
        ----------
        filename : str or `~pathlib.Path`
            Name of the FITS file.
        hdu : str
            Name or index of the HDU with the map data.
        hdu_bands : str
            Name or index of the HDU with the BANDS table.  If not
            defined this will be inferred from the FITS header of the
            map HDU.

        Returns
        -------
        geom : `~MapGeom`
            Geometry object.
        """
        filename = str(make_path(filename))
        with fits.open(filename, memmap=False) as hdulist:
            geom = cls.from_hdulist(hdulist, **kwargs)
        return geom

    @classmethod
    def from_hdulist(cls, hdulist, hdu=None, hdu_bands=None):
        """Load a geometry object from a FITS HDUList.

        Parameters
        ----------
        hdulist :  `~astropy.io.fits.HDUList`
            HDU list containing HDUs for map data and bands.
        hdu : str
            Name or index of the HDU with the map data.
        hdu_bands : str
            Name or index of the HDU with the BANDS table.  If not
            defined this will be inferred from the FITS header of the
            map HDU.

        Returns
        -------
        geom : `~MapGeom`
            Geometry object.
        """
        if hdu is None:
            hdu = find_hdu(hdulist)
        else:
            hdu = hdulist[hdu]

        if hdu_bands is None:
            hdu_bands = find_bands_hdu(hdulist, hdu)

        if hdu_bands is not None:
            hdu_bands = hdulist[hdu_bands]

        return cls.from_header(hdu.header, hdu_bands)

    def make_bands_hdu(self, hdu=None, hdu_skymap=None, conv=None):
        conv = self.conv if conv is None else conv
        header = fits.Header()
        self._fill_header_from_axes(header)
        axis_names = None

        # FIXME: Check whether convention is compatible with
        # dimensionality of geometry

        if conv == 'fgst-ccube':
            hdu = 'EBOUNDS'
            axis_names = ['energy']
        elif conv == 'fgst-template':
            hdu = 'ENERGIES'
            axis_names = ['energy']
        elif conv == 'gadf' and hdu is None:
            if hdu_skymap:
                hdu = '{}_{}'.format(hdu_skymap, 'BANDS')
            else:
                hdu = 'BANDS'
        # else:
        #     raise ValueError('Unknown conv: {}'.format(conv))

        cols = make_axes_cols(self.axes, axis_names)
        cols += self._make_bands_cols()
        return fits.BinTableHDU.from_columns(cols, header, name=hdu)

    @abc.abstractmethod
    def _make_bands_cols(self):
        pass

    @abc.abstractmethod
    def get_idx(self, idx=None, local=False, flat=False):
        """Get tuple of pixel indices for this geometry.

        Returns all pixels in the geometry by default. Pixel indices
        for a single image plane can be accessed by setting ``idx``
        to the index tuple of a plane.

        Parameters
        ----------
        idx : tuple, optional
            A tuple of indices with one index for each non-spatial
            dimension.  If defined only pixels for the image plane with
            this index will be returned.  If none then all pixels
            will be returned.

        local : bool
            Flag to return local or global pixel indices.  Local
            indices run from 0 to the number of pixels in a given
            image plane.

        flat : bool, optional
            Return a flattened array containing only indices for
            pixels contained in the geometry.

        Returns
        -------
        idx : tuple
            Tuple of pixel index vectors with one vector for each
            dimension.
        """
        pass

    @abc.abstractmethod
    def get_coord(self, idx=None, flat=False):
        """Get the coordinate array for this geometry.

        Returns a coordinate array with the same shape as the data
        array.  Pixels outside the geometry are set to NaN.
        Coordinates for a single image plane can be accessed by
        setting ``idx`` to the index tuple of a plane.

        Parameters
        ----------
        idx : tuple, optional
            A tuple of indices with one index for each non-spatial
            dimension.  If defined only coordinates for the image
            plane with this index will be returned.  If none then
            coordinates for all pixels will be returned.

        flat : bool, optional
            Return a flattened array containing only coordinates for
            pixels contained in the geometry.

        Returns
        -------
        coords : tuple
            Tuple of coordinate vectors with one vector for each
            dimension.
        """
        pass

    @abc.abstractmethod
    def coord_to_pix(self, coords):
        """Convert map coordinates to pixel coordinates.

        Parameters
        ----------
        coords : tuple
            Coordinate values in each dimension of the map.  This can
            either be a tuple of numpy arrays or a MapCoord object.
            If passed as a tuple then the ordering should be
            (longitude, latitude, c_0, ..., c_N) where c_i is the
            coordinate vector for axis i.

        Returns
        -------
        pix : tuple
            Tuple of pixel coordinates in image and band dimensions.
        """
        pass

    def coord_to_idx(self, coords, clip=False):
        """Convert map coordinates to pixel indices.

        Parameters
        ----------
        coords : tuple or `~MapCoord`
            Coordinate values in each dimension of the map.  This can
            either be a tuple of numpy arrays or a MapCoord object.
            If passed as a tuple then the ordering should be
            (longitude, latitude, c_0, ..., c_N) where c_i is the
            coordinate vector for axis i.
        clip : bool
            Choose whether to clip indices to the valid range of the
            geometry.  If false then indices for coordinates outside
            the geometry range will be set -1.

        Returns
        -------
        pix : tuple
            Tuple of pixel indices in image and band dimensions.
            Elements set to -1 correspond to coordinates outside the
            map.
        """
        pix = self.coord_to_pix(coords)
        return self.pix_to_idx(pix, clip=clip)

    @abc.abstractmethod
    def pix_to_coord(self, pix):
        """Convert pixel coordinates to map coordinates.

        Parameters
        ----------
        pix : tuple
            Tuple of pixel coordinates.

        Returns
        -------
        coords : tuple
            Tuple of map coordinates.
        """
        pass

    @abc.abstractmethod
    def pix_to_idx(self, pix, clip=False):
        """Convert pixel coordinates to pixel indices.  Returns -1 for pixel
        coordinates that lie outside of the map.

        Parameters
        ----------
        pix : tuple
            Tuple of pixel coordinates.
        clip : bool
            Choose whether to clip indices to the valid range of the
            geometry.  If false then indices for coordinates outside
            the geometry range will be set -1.

        Returns
        -------
        idx : tuple
            Tuple of pixel indices.
        """
        pass

    @abc.abstractmethod
    def contains(self, coords):
        """Check if a given map coordinate is contained in the geometry.

        Parameters
        ----------
        coords : tuple or `~gammapy.maps.MapCoord`
            Tuple of map coordinates.

        Returns
        -------
        containment : `~numpy.ndarray`
            Bool array.
        """
        pass

    def contains_pix(self, pix):
        """Check if a given pixel coordinate is contained in the geometry.

        Parameters
        ----------
        pix : tuple
            Tuple of pixel coordinates.

        Returns
        -------
        containment : `~numpy.ndarray`
            Bool array.
        """
        idx = self.pix_to_idx(pix)
        return np.all(np.stack([t != -1 for t in idx]), axis=0)

    def slice_by_idx(self, slices):
        """Create a new geometry by cutting in the non-spatial dimensions of
        this geometry.

        Parameters
        ----------
        slices : dict
            Dict of axes names and integers or `slice` object pairs. Contains one
            element for each non-spatial dimension. For integer indexing the
            correspoding axes is dropped from the map. Axes not specified in the
            dict are kept unchanged.

        Returns
        -------
        geom : `~MapGeom`
            Sliced geometry.
        """
        axes = []
        for ax in self.axes:
            ax_slice = slices.get(ax.name, slice(None))
            if isinstance(ax_slice, slice):
                ax_sliced = ax.slice(ax_slice)
                axes.append(ax_sliced)
                # in the case where isinstance(ax_slice, int) the axes is dropped

        kwargs = self._copy_init_kwargs
        kwargs['axes'] = axes
        return self.__class__(**kwargs)

    @abc.abstractmethod
    def to_image(self):
        """Create a 2D geometry by dropping all non-spatial dimensions of this
        geometry.

        Returns
        -------
        geom : `~MapGeom`
            Image geometry.
        """
        pass

    @abc.abstractmethod
    def to_cube(self, axes):
        """Create a new geometry by appending a list of non-spatial axes to
        the present geometry.  This will result in a new geometry with
        N+M dimensions where N is the number of current dimensions and
        M is the number of axes in the list.

        Parameters
        ----------
        axes : list
            Axes that will be appended to this geometry.

        Returns
        -------
        geom : `~MapGeom`
            Map geometry.
        """
        pass

    def coord_to_tuple(self, coord):
        """Generate a coordinate tuple compatible with this geometry.

        Parameters
        ----------
        coord : `~MapCoord`
        """
        if self.ndim != coord.ndim:
            raise ValueError

        if not coord.match_by_name:
            return tuple(coord._data.values())

        coord_tuple = [coord.lon, coord.lat]
        for ax in self.axes:
            coord_tuple += [coord[ax.name]]

        return coord_tuple

    @abc.abstractmethod
    def pad(self, pad_width):
        """
        Pad the geometry at the edges.

        Parameters
        ----------
        pad_width : {sequence, array_like, int}
            Number of values padded to the edges of each axis.

        Returns
        -------
        geom : `~MapGeom`
            Padded geometry.
        """
        pass

    @abc.abstractmethod
    def crop(self, crop_width):
        """
        Crop the geometry at the edges.

        Parameters
        ----------
        crop_width : {sequence, array_like, int}
            Number of values cropped from the edges of each axis.

        Returns
        -------
        geom : `~MapGeom`
            Cropped geometry.
        """
        pass

    @abc.abstractmethod
    def downsample(self, factor):
        """Downsample the spatial dimension of the geometry by a given factor.

        Parameters
        ----------
        factor : int
            Downsampling factor.

        Returns
        -------
        geom : `~MapGeom`
            Downsampled geometry.

        """
        pass

    @abc.abstractmethod
    def upsample(self, factor):
        """Upsample the spatial dimension of the geometry by a given factor.

        Parameters
        ----------
        factor : int
            Upsampling factor.

        Returns
        -------
        geom : `~MapGeom`
            Upsampled geometry.

        """
        pass

    @abc.abstractmethod
    def solid_angle(self):
        """Solid angle (`~astropy.units.Quantity` in ``sr``)."""
        pass

    def _fill_header_from_axes(self, header):

        for i, ax in enumerate(self.axes):

            if ax.name == 'energy' and ax.node_type == 'edge':
                header['AXCOLS%i' % (i + 1)] = 'E_MIN,E_MAX'
            elif ax.name == 'energy' and ax.node_type == 'center':
                header['AXCOLS%i' % (i + 1)] = 'ENERGY'
            elif ax.node_type == 'edge':
                header['AXCOLS%i' % (i + 1)] = '{}_MIN,{}_MAX'.format(ax.name.upper(),
                                                                      ax.name.upper())
            elif ax.node_type == 'center':
                header['AXCOLS%i' % (i + 1)] = ax.name.upper()
            else:
                raise ValueError('Invalid node type '
                                 '{}'.format(ax.node_type))

    @property
    def is_image(self):
        """Whether the geom is equivalent to an image without extra dimensions."""
        if self.axes is None:
            return True
        is_image = len(self.axes) == 0
        return is_image

    @property
    def axes_names(self):
        """Returns list of axes names"""
        return [_.name for _ in self.axes]

    def get_axis_by_name(self, name):
        """Return axis with corresponding name

        Parameters
        ----------
        name : str
           the name of the requested axis

        Returns
        -------
        axis : `~gammapy.maps.MapAxis`
            the corresponding axis

        """

        # TODO : we implictly assume all axes have different names. This should be enforced at MapGeom creation.
        for i, axis in enumerate(self.axes):
            if axis.name.upper() == name.upper():
                return axis
        raise ValueError("Cannot find axis named {}".format(name))

    def get_axis_by_type(self, type):
        """Returns axis of given type.

        Parameters
        ----------
        type : str in {'energy', 'time', 'any'}
           the name of the requested type of axis

        Returns
        -------
        axes : `~gammapy.maps.MapAxis`
            the corresponding  axis
        """
        valid_types = ('energy', 'time', 'any')
        if type not in valid_types:
            raise ValueError("Invalid axis type {}. Should be {}.".format(type, valid_types))

        for i, axis in enumerate(self.axes):
            if axis.type == type:
                return axis
        raise ValueError("Cannot find type {}".format(type))
