import os
import sys
import numpy as np
import pandas as pd
import openmc.data


def get_temp(path):
    _temp_key = path[-3]
    temp_dict = {
        '0': '294K',
        '1': '600K',
        '2': '900K',
        '3': '1200K',
        '4': '2500K',
        '5': '0K',
        '6': '250K',
    }
    return temp_dict[_temp_key]


def export_xs_data(path):
    ace = openmc.data.IncidentNeutron.from_ace(path, metastable_scheme='mcnp')
    temp = get_temp(path)
    # df1 = pd.DataFrame()
    df = pd.DataFrame()
    _energy = ace.energy[temp]
    _total_xs = ace[1].xs[temp](_energy)
    #     df[ace.atomic_symbol] = ['E_eV']
    #     df[ace.mass_number] = ['Sig_b']
    #     df1[ace.atomic_symbol] = _energy
    #     df1[ace.mass_number] = _total_xs
    #     df = df.append(df1)
    df['E_eV'] = _energy
    df['Sig_b'] = _total_xs
    name = ace.name
    if '_' in name:
        fname = ace.atomic_symbol + '-' + str(ace.mass_number) + '_' + name.split('_')[-1] + '.csv'
    else:
        fname = ace.atomic_symbol + '-' + str(ace.mass_number) + '.csv'
    sub_dir = temp
    if not os.path.exists(sub_dir):
        os.makedirs(sub_dir)
    df.to_csv(temp + '/' + fname, index=False, float_format='%g')


loc = pd.read_csv('/Users/y9z/Documents/database/Lib80x/xsdir', '/t', header=None)

cwd = '/Users/y9z/Documents/database/Lib80x/'

for each in loc[0]:
    if each.count(' ') > 1:
        sp = each.split(' ')
        path = cwd + sp[2]

        if '/Lib80x/Lib80x/B/5010.8' in path:
            print(path)
            export_xs_data(path)
