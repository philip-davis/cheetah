"""
Functions for parsing and editing the ADIOS xml file to enable variable transforms.
Transforms include compression and reduction. 'Transform' is an ADIOS specific term.

Not making this a class right now.
"""

import xml.etree.cElementTree as ET

def adios_xml_transform(xml_filepath, group_name, var_name, value):
    """
    Edit the ADIOS XML file to enable transform (compression/reduction) for a variable
    
    :param varName:     Name of the variable that will be transformed
    :param value:       Transform type (sz, zfp etc.)
    :param xmlFilepath: Full path of the adios xml file. This will be in the run directory.
    :return:            success or error. Return error if variable not found.
    """

    tree = ET.parse(xml_filepath)
    root = tree.getroot()

    tag = tree.find('adios-group[@name="%s"]/global-bounds/var[@name="%s"]' % (group_name, var_name))
    tag.set('transform', value)
    tree.write(xml_filepath)


#if __name__ == "__main__":
 #   adios_xml_transform("heat:T", "sz", "/Users/kpu/vshare/scratch/heat_transfer.xml")